"""Azeroth Survivors trainer — window.

  python gui.py          (or double-click gui.bat in the repo root)

Click 定位游戏 inside a run, wait for the sweep (30-90 s), then:
  * 属性 (stat cache): type a value, tick 锁定 (or press Enter) — the game
    recomputes these every 1-3 s, so only a lock sticks.
  * 状态 (run state): type a value, click 写入 (or press Enter). Tick 锁定 to
    keep it there (e.g. chest price 1, rerolls 99).
  * 存档 (save file, via save.py): perk resets and marks. Only writable while
    the game is closed — the game rewrites the save when it exits.
"""
import ctypes
import queue
import threading
import tkinter as tk
from tkinter import ttk

import game
import save
from offsets import GAME_VERSION
from session import FIELDS, SPEC, UNIT, Session, SessionError, parse, show

REFRESH_MS = 300
LOST_AFTER = 3          # unreadable refreshes in a row before we call the target gone


class App:
    def __init__(self, root):
        self.root = root
        self.session = Session()
        self.results = queue.Queue()
        self.misses = 0
        self.locating = False
        self.rows = {}          # (kind, name) -> {"cur", "entry", "lock"}

        root.title(f"Azeroth Survivors Trainer  ·  {GAME_VERSION}")
        root.resizable(False, False)
        style = ttk.Style()
        style.configure("Locked.TLabel", foreground="#1a7f37")
        style.configure("Err.TLabel", foreground="#cf222e")

        top = ttk.Frame(root, padding=(10, 10, 10, 0))
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.locate_btn = ttk.Button(top, text="定位游戏", command=self.on_locate_button)
        self.locate_btn.pack(side="left")
        self.bar = ttk.Progressbar(top, mode="indeterminate", length=120)
        self.status = tk.StringVar(value="进入一局后点“定位游戏”")
        ttk.Label(top, textvariable=self.status).pack(side="left", padx=10)

        stat = ttk.LabelFrame(root, text="属性（和游戏角色面板一样的写法；勾“锁定”才持续生效）", padding=8)
        state = ttk.LabelFrame(root, text="状态（写一次就生效）", padding=8)
        stat.grid(row=1, column=0, sticky="n", padx=(10, 5), pady=10)
        state.grid(row=1, column=1, sticky="n", padx=(5, 10), pady=10)
        for frame in (stat, state):
            for col, text in enumerate(("", "当前", "设为")):
                ttk.Label(frame, text=text, foreground="#666").grid(row=0, column=col, sticky="w")

        counts = {"stat": 0, "player": 0}
        last_group = None
        for kind, name, label, _fmt, lockable, unit, group in FIELDS:
            col = "player" if kind == "player" else "stat"   # effects sit with the stats
            frame = state if col == "player" else stat
            counts[col] += 1
            if col == "stat" and group != last_group:       # 角色 / 冒险属性, as in the game
                ttk.Label(frame, text=group, foreground="#9a6700").grid(
                    row=counts[col], column=0, sticky="w", pady=(6, 0))
                counts[col] += 1
            last_group = group
            self.add_row(frame, counts[col], kind, name, label, unit, lockable)

        ttk.Button(state, text="回满血", command=self.heal).grid(
            row=counts["player"] + 1, column=0, sticky="w", pady=(8, 0))

        self.save_panel = SavePanel(root, self)
        self.save_panel.frame.grid(row=2, column=0, columnspan=2, sticky="ew",
                                   padx=10, pady=(0, 10))

        bottom = ttk.Frame(root, padding=(10, 0, 10, 10))
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew")
        ttk.Button(bottom, text="全部解锁", command=self.unlock_all).pack(side="left")
        self.msg = tk.StringVar()
        self.msg_label = ttk.Label(bottom, textvariable=self.msg)
        self.msg_label.pack(side="left", padx=10)

        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.set_enabled(False)
        root.after(REFRESH_MS, self.refresh)

    # --- layout -------------------------------------------------------------
    def add_row(self, frame, r, kind, name, label, unit, lockable):
        key = (kind, name)
        ttk.Label(frame, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8))
        cur = ttk.Label(frame, text="-", width=10, anchor="e")
        cur.grid(row=r, column=1, sticky="e", padx=(0, 8))
        entry = ttk.Entry(frame, width=10)
        entry.grid(row=r, column=2, pady=1)
        entry.bind("<Return>", lambda _e: self.on_enter(key))
        lock = tk.BooleanVar(value=False)
        row = {"cur": cur, "entry": entry, "lock": lock, "unit": unit, "widgets": [entry]}
        col = 3
        if kind == "player":
            b = ttk.Button(frame, text="写入", width=5, command=lambda: self.write(key))
            b.grid(row=r, column=col, padx=(4, 0))
            row["widgets"].append(b)
            col += 1
        if lockable:
            c = ttk.Checkbutton(frame, text="锁定", variable=lock,
                                command=lambda: self.toggle_lock(key))
            c.grid(row=r, column=col, padx=(4, 0))
            row["widgets"].append(c)
        self.rows[key] = row

    def set_enabled(self, on):
        for row in self.rows.values():
            for w in row["widgets"]:
                w.state(["!disabled"] if on else ["disabled"])

    def say(self, text, error=False):
        self.msg.set(text)
        self.msg_label.configure(style="Err.TLabel" if error else "TLabel")

    # --- actions ------------------------------------------------------------
    def on_locate_button(self):
        # once connected the button reads 重新定位: find the hero again, keep the locks
        if self.session.h is not None and self.session.base is not None:
            self.relocate_async("重新定位中（锁定保留）…")
        else:
            self.locate()

    def relocate_async(self, text):
        self.locating = True
        self.locate_btn.state(["disabled"])
        self.bar.pack(side="left", padx=(10, 0))
        self.bar.start(12)
        self.status.set(text)

        def work():
            try:
                self.results.put(("relocated", self.session.relocate()))
            except SessionError as e:
                self.results.put(("err", str(e)))
            except Exception as e:      # noqa: BLE001
                self.results.put(("err", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(100, self.poll_locate)

    def locate(self):
        self.unlock_all(quiet=True)
        self.set_enabled(False)
        self.locating = True
        self.locate_btn.state(["disabled"])
        self.bar.pack(side="left", padx=(10, 0))
        self.bar.start(12)
        self.status.set("扫描内存中（约 30～90 秒）…")
        self.say("")

        def work():
            try:
                self.results.put(("ok", self.session.locate()))
            except SessionError as e:
                self.results.put(("err", str(e)))
            except Exception as e:      # noqa: BLE001 — show anything, never die silently
                self.results.put(("err", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(100, self.poll_locate)

    def poll_locate(self):
        try:
            kind, val = self.results.get_nowait()
        except queue.Empty:
            self.root.after(100, self.poll_locate)
            return
        self.locating = False
        self.bar.stop()
        self.bar.pack_forget()
        self.locate_btn.state(["!disabled"])
        if kind == "ok":
            self.misses = 0
            self.status.set(f"已连接  pid {self.session.pid}  ·  玩家对象 0x{val:X}")
            self.locate_btn.configure(text="重新定位")
            self.set_enabled(True)
        elif kind == "relocated":
            old, new = val
            self.misses = 0
            self.status.set(f"已连接  pid {self.session.pid}  ·  玩家对象 0x{new:X}")
            self.say("角色位置没变" if old == new else
                     f"角色在内存里换了位置，已重新连上，锁定保留（{len(self.session.pinned())} 项）")
        elif self.session.base is not None:
            self.disconnect(val)               # relocate failed: start over by hand
        else:
            self.status.set("没有连上")
            self.say(val, error=True)

    def value_for(self, key):
        """Raw value from the entry, typed the way the game writes it ('+50%',
        '50'); the current value if the entry is empty."""
        row = self.rows[key]
        text = row["entry"].get().strip()
        if not text:
            text = row["cur"].cget("text")
            row["entry"].insert(0, text)
        try:
            return parse(UNIT[key], text)
        except ValueError:
            raise ValueError(f"看不懂“{text}”：填数字，比如 50 或 +50%") from None

    def write(self, key):
        try:
            if self.rows[key]["lock"].get():
                v = self.session.pin(*key, self.value_for(key))     # writes it too
            else:
                v = self.session.write(*key, self.value_for(key))
            self.say(f"{SPEC[key][0]} → {show(UNIT[key], v)}")
        except (ValueError, SessionError) as e:
            self.say(str(e), error=True)

    def toggle_lock(self, key):
        row = self.rows[key]
        try:
            if row["lock"].get():
                v = self.session.pin(*key, self.value_for(key))     # writes it now
                self.say(f"已锁定 {SPEC[key][0]} = {show(UNIT[key], v)}")
            else:
                self.session.pin(*key, None)
                self.say(f"已解锁 {SPEC[key][0]}")
        except (ValueError, SessionError) as e:
            row["lock"].set(False)
            self.say(str(e), error=True)

    def on_enter(self, key):
        row = self.rows[key]
        if key[0] == "player":
            self.write(key)
        elif row["lock"].get():
            self.toggle_lock(key)               # re-pin with the new value
        else:
            row["lock"].set(True)               # Enter on a stat = lock it
            self.toggle_lock(key)

    def heal(self):
        try:
            self.say(f"血量回满 {self.session.heal():.0f}")
        except SessionError as e:
            self.say(str(e), error=True)

    def unlock_all(self, quiet=False):
        for key, row in self.rows.items():
            if row["lock"].get():
                row["lock"].set(False)
                self.session.pin(*key, None)
        if not quiet:
            self.say("全部解锁")

    # --- refresh loop -------------------------------------------------------
    def refresh(self):
        self.root.after(REFRESH_MS, self.refresh)
        self.save_panel.tick()
        if self.locating or self.session.base is None:
            return
        vals = None if self.session.lost else self.session.read()
        while self.session.events:
            self.say(self.session.events.pop(0))
            self.status.set(f"已连接  pid {self.session.pid}  ·  玩家对象 0x{self.session.base:X}")
        if vals is None:
            self.misses += 1
            if self.session.lost or self.misses >= LOST_AFTER:
                # usually the game moved the hero (Bevy relocates an entity when its
                # components change): go after it, locks stay
                self.relocate_async("角色位置变了，正在自动重新定位（锁定保留）…")
            return
        self.misses = 0
        pinned = self.session.pinned()
        for key, row in self.rows.items():
            row["cur"].configure(text=show(row["unit"], vals[key]),
                                 style="Locked.TLabel" if key in pinned else "TLabel")

    def disconnect(self, why=""):
        self.unlock_all(quiet=True)
        self.session.close()
        self.set_enabled(False)
        for row in self.rows.values():
            row["cur"].configure(text="-", style="TLabel")
        self.status.set("连不上角色了（游戏关了或对局结束？）")
        self.locate_btn.configure(text="定位游戏")
        self.say((why + " · " if why else "") + "进入一局后重新点“定位游戏”", error=True)

    def quit(self):
        self.session.close()
        self.root.destroy()


GAME_CHECK_EVERY = 3    # refresh ticks (~1 s) between "is the game running?" checks


class SavePanel:
    """Out-of-run progress in the save file. Writable only while the game is closed."""

    def __init__(self, root, app):
        self.root, self.app = root, app
        self.sv = None                  # save.Save of the last successful read
        self.loading = False
        self.results = queue.Queue()
        self.game_running = None
        self.ticks = 0
        self.classes = []               # class ids in the combobox, same order

        f = self.frame = ttk.LabelFrame(
            root, text="存档（局外进度 · 只有游戏完全退出后才能改，每次自动备份）", padding=8)
        self.info = tk.StringVar(value="读取中…")
        ttk.Label(f, textvariable=self.info).grid(row=0, column=0, columnspan=5, sticky="w")
        self.reload_btn = ttk.Button(f, text="重新读取", command=self.load)
        self.reload_btn.grid(row=0, column=5, sticky="e")

        ttk.Label(f, text="特权重置").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.resets_cur = ttk.Label(f, text="-", width=14)
        self.resets_cur.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self.resets_btn = ttk.Button(f, text="清零（恢复全部重置）", command=self.clear_resets)
        self.resets_btn.grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(f, text="印记").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.cls = ttk.Combobox(f, state="readonly", width=12)
        self.cls.grid(row=2, column=1, sticky="w", pady=(4, 0))
        self.cls.bind("<<ComboboxSelected>>", lambda _e: self.show_marks())
        self.marks_cur = ttk.Label(f, text="-", width=14, anchor="e")
        self.marks_cur.grid(row=2, column=2, sticky="e", padx=(0, 8), pady=(4, 0))
        self.marks_entry = ttk.Entry(f, width=10)
        self.marks_entry.grid(row=2, column=3, pady=(4, 0))
        self.marks_entry.bind("<Return>", lambda _e: self.write_marks())
        self.marks_btn = ttk.Button(f, text="写入", width=5, command=self.write_marks)
        self.marks_btn.grid(row=2, column=4, padx=(4, 0), pady=(4, 0))

        self.hint = ttk.Label(f, text="")
        self.hint.grid(row=3, column=0, columnspan=6, sticky="w", pady=(6, 0))
        self.load()

    # --- read -------------------------------------------------------------
    def load(self):
        if self.loading:
            return
        self.loading = True
        self.info.set("读取存档中…")
        self.update_state()

        def work():
            try:
                self.results.put(("ok", save.Save()))
            except save.SaveError as e:
                self.results.put(("err", str(e)))
            except Exception as e:      # noqa: BLE001
                self.results.put(("err", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def take_result(self):
        try:
            kind, val = self.results.get_nowait()
        except queue.Empty:
            return
        self.loading = False
        if kind == "err":
            self.sv = None
            self.info.set(f"读不了存档：{val}")
        else:
            self.sv = val
            self.info.set(f"存档：{self.sv.path}")
        self.show()

    def show(self):
        if not self.sv:
            self.resets_cur.configure(text="-")
            self.marks_cur.configure(text="-")
            self.update_state()
            return
        lines = self.sv.lines
        self.resets_cur.configure(text=f"已用 {save.resets_used(lines)} 次")
        marks = save.all_marks(lines)
        try:
            current = save.current_class(lines)
        except save.SaveError:
            current = None
        # every class, plus any id the save has that we don't know a name for
        self.classes = sorted(set(save.CLASS_NAMES) | set(marks) | ({current} if current else set()))
        self.cls.configure(values=[f"{save.CLASS_NAMES.get(c, '职业')} ({c})" for c in self.classes])
        if self.classes and self.cls.current() < 0:
            self.cls.current(self.classes.index(current) if current in self.classes else 0)
        self.show_marks()
        self.update_state()

    def show_marks(self):
        i = self.cls.current()
        if not self.sv or i < 0:
            self.marks_cur.configure(text="-")
            return
        v = save.all_marks(self.sv.lines).get(self.classes[i])
        self.marks_cur.configure(text="0.0（无记录）" if v is None else f"{v:.1f}")

    # --- game running? ----------------------------------------------------
    def tick(self):
        self.take_result()
        self.ticks += 1
        if self.ticks % GAME_CHECK_EVERY and self.game_running is not None:
            return
        running = bool(game.find_pid())
        if running == self.game_running:
            return
        was = self.game_running
        self.game_running = running
        if was and not running:
            self.load()                 # the game rewrote the save on its way out
        self.update_state()

    def update_state(self):
        can = bool(self.sv) and self.game_running is False and not self.loading
        for w in (self.resets_btn, self.marks_btn, self.marks_entry):
            w.state(["!disabled"] if can else ["disabled"])
        if self.game_running:
            self.hint.configure(text="游戏正在运行：存档只读。完全退出游戏后这里会自动变成可改。",
                                style="Err.TLabel")
        else:
            self.hint.configure(text="改完再启动游戏；备份在存档旁边的 trainer-backups 文件夹。",
                                style="TLabel")

    # --- write ------------------------------------------------------------
    def commit(self, new_lines, what):
        try:
            backup = self.sv.commit(new_lines)
        except save.SaveError as e:
            self.app.say(str(e), error=True)
            return
        if backup is None:
            self.app.say(f"{what}：没有变化")
        else:
            self.app.say(f"{what}：已写入并核对，备份 {backup}")
        self.load()

    def clear_resets(self):
        if self.sv:
            self.commit(save.set_resets(self.sv.lines, 0), "特权重置清零")

    def write_marks(self):
        i = self.cls.current()
        if not self.sv or i < 0:
            return
        try:
            value = float(self.marks_entry.get().strip())
            new = save.set_marks(self.sv.lines, self.classes[i], value)
        except (ValueError, save.SaveError) as e:
            self.app.say(f"印记：{e}", error=True)
            return
        self.commit(new, f"{save.CLASS_NAMES.get(self.classes[i], '')}印记 → {value:.1f}")


def main():
    game.utf8_console()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)     # crisp text on scaled displays
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
