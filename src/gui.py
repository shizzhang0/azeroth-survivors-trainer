"""Azeroth Survivors trainer — window.

  python gui.py          (or double-click gui.bat in the repo root)

Click 定位游戏 inside a run, wait for the sweep (30-90 s), then:
  * 属性 (stat cache): type a value, tick 锁定 (or press Enter) — the game
    recomputes these every 1-3 s, so only a lock sticks.
  * 状态 (run state): type a value, click 写入 (or press Enter). Tick 锁定 to
    keep it there (e.g. chest price 1, rerolls 99). 回满血 fills health up to
    生命值 once; 锁满血 keeps it there (raise 生命值 with a lock for more).
  * 极速模式: the run timer (what bosses go by) at 2x / 4x / 8x; combat keeps its
    speed. The timer sits in the player object (turbo.py).
  * 存档 (save file, via save.py): perk resets and marks. Only writable while
    the game is closed — the game rewrites the save when it exits. The key is
    in the game exe: if AZS_EXE isn't set, it asks for the exe once and
    remembers it (scan_stores.remember_exe).
"""
import ctypes
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import game
import save
import scan_stores
import turbo
from session import FIELDS, SPEC, UNIT, Session, SessionError, parse, show

REFRESH_MS = 300
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
LOST_AFTER = 3          # unreadable refreshes in a row before we call the target gone

# --- look ---------------------------------------------------------------------
BG = "#eef0f3"          # window
CARD = "#ffffff"
BORDER = "#dde1e6"
TEXT = "#1f2328"
MUTED = "#6b7280"
ACCENT = "#b7791f"      # amber, a nod to the game's gold
ACCENT_DARK = "#975a16"
GOOD = "#15803d"
BAD = "#b42318"
FONT = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 14, "bold")
FONT_CARD = ("Microsoft YaHei UI", 11, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)


def apply_style(root):
    """One light theme for the whole window: white cards on grey, amber accent."""
    root.configure(background=BG)
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", font=FONT, background=CARD, foreground=TEXT, bordercolor=BORDER,
                lightcolor=CARD, darkcolor=CARD, troughcolor=BG, focuscolor=ACCENT,
                selectbackground=ACCENT, selectforeground="white")
    s.configure("TFrame", background=BG)
    s.configure("Card.TFrame", background=CARD, bordercolor=BORDER, lightcolor=BORDER,
                darkcolor=BORDER, relief="solid", borderwidth=1)
    s.configure("Body.TFrame", background=CARD)
    s.configure("TLabel", background=CARD, foreground=TEXT)
    s.configure("App.TLabel", background=BG, foreground=TEXT, font=FONT_TITLE)
    s.configure("AppMuted.TLabel", background=BG, foreground=MUTED)
    s.configure("AppGood.TLabel", background=BG, foreground=GOOD, font=FONT_BOLD)
    s.configure("Msg.TLabel", background=BG, foreground=MUTED)
    s.configure("MsgErr.TLabel", background=BG, foreground=BAD)
    s.configure("CardTitle.TLabel", font=FONT_CARD)
    s.configure("Muted.TLabel", foreground=MUTED, font=FONT_SMALL)
    s.configure("Group.TLabel", foreground=ACCENT_DARK, font=FONT_BOLD)
    s.configure("Value.TLabel", font=FONT_BOLD)
    s.configure("LockedValue.TLabel", font=FONT_BOLD, foreground=GOOD)
    s.configure("Locked.TLabel", foreground=GOOD, font=FONT_BOLD)
    s.configure("Err.TLabel", foreground=BAD)
    s.configure("TSeparator", background=BORDER)

    s.configure("TButton", background="#f6f7f9", bordercolor=BORDER, lightcolor="#f6f7f9",
                darkcolor="#f6f7f9", padding=(12, 3), focusthickness=0)
    s.map("TButton", background=[("disabled", "#f6f7f9"), ("pressed", "#e4e7eb"), ("active", "#eceff2")],
          foreground=[("disabled", "#a0a6ae")])
    s.configure("Accent.TButton", background=ACCENT, foreground="white", bordercolor=ACCENT,
                lightcolor=ACCENT, darkcolor=ACCENT, font=FONT_BOLD, padding=(16, 4))
    s.map("Accent.TButton",
          background=[("disabled", "#d9c39c"), ("pressed", ACCENT_DARK), ("active", ACCENT_DARK)],
          foreground=[("disabled", "white")])
    s.configure("Small.TButton", padding=(8, 1))

    s.configure("TEntry", fieldbackground="white", bordercolor=BORDER, lightcolor="white",
                darkcolor="white", padding=(6, 2))
    s.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
          fieldbackground=[("disabled", "#f6f7f9")])
    s.configure("TCombobox", fieldbackground="white", background="white", bordercolor=BORDER,
                lightcolor="white", darkcolor="white", arrowcolor=MUTED, padding=(6, 2))
    s.map("TCombobox", fieldbackground=[("readonly", "white")], bordercolor=[("focus", ACCENT)])
    s.configure("TCheckbutton", background=CARD, indicatorbackground="white", indicatorsize=15,
                indicatormargin=(0, 0, 6, 0), indicatorforeground="white",
                upperbordercolor="#b9bec5", lowerbordercolor="#b9bec5")
    s.map("TCheckbutton", indicatorbackground=[("selected", ACCENT), ("disabled", "#f1f2f4")],
          background=[("active", CARD)], foreground=[("disabled", "#a0a6ae")])

    # 极速模式: a segmented control (radio buttons drawn as buttons)
    s.configure("Seg.Toolbutton", background="#f3f4f6", bordercolor=BORDER, lightcolor="#f3f4f6",
                darkcolor="#f3f4f6", padding=(16, 4), anchor="center", font=FONT_BOLD)
    s.map("Seg.Toolbutton",
          background=[("disabled", "#f6f7f9"), ("selected", ACCENT), ("active", "#e9ebee")],
          foreground=[("disabled", "#a0a6ae"), ("selected", "white")],
          bordercolor=[("selected", ACCENT)], lightcolor=[("selected", ACCENT)],
          darkcolor=[("selected", ACCENT)])
    s.configure("TProgressbar", background=ACCENT, troughcolor=BORDER, bordercolor=BG,
                lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)


def set_icon(root):
    """Window and taskbar icon (make_icon.py). Tk's own iconbitmap always takes the 16 px
    picture and stretches it (24 px title bar at 150%: blurry), so on Windows we ask for
    the exact sizes the system wants and hand them to the window ourselves."""
    try:
        root.iconbitmap(default=ICON)           # elsewhere, and the fallback
    except tk.TclError:
        return
    try:
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        u32.LoadImageW.restype = wintypes.HANDLE
        u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        root.update_idletasks()
        hwnd = u32.GetParent(root.winfo_id())
        IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
        for which, metric in ((0, 49), (1, 11)):        # ICON_SMALL / SM_CXSMICON, ICON_BIG / SM_CXICON
            n = u32.GetSystemMetrics(metric)
            icon = u32.LoadImageW(None, ICON, IMAGE_ICON, n, n, LR_LOADFROMFILE)
            if icon:
                u32.SendMessageW(hwnd, WM_SETICON, which, icon)   # kept for the window's life
    except (AttributeError, OSError):
        pass


def card(parent, title, note=""):
    """A white card with a bold title (and a muted note). Returns (card, header, body)."""
    outer = ttk.Frame(parent, style="Card.TFrame", padding=(16, 12, 16, 14))
    head = ttk.Frame(outer, style="Body.TFrame")
    head.pack(fill="x")
    ttk.Label(head, text=title, style="CardTitle.TLabel").pack(side="left")
    if note:
        ttk.Label(head, text=note, style="Muted.TLabel").pack(side="left", padx=(10, 0), pady=(3, 0))
    body = ttk.Frame(outer, style="Body.TFrame")
    body.pack(fill="both", expand=True, pady=(10, 0))
    return outer, head, body


class App:
    def __init__(self, root):
        self.root = root
        self.session = Session()
        self.results = queue.Queue()
        self.misses = 0
        self.locating = False
        self.rows = {}          # (kind, name) -> {"cur", "entry", "lock"}

        root.title("Azeroth Survivors 修改器")
        root.resizable(False, False)
        set_icon(root)
        apply_style(root)

        # header: title, connection status, the one big button
        top = ttk.Frame(root, padding=(18, 14, 18, 4))
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(top, text="Azeroth Survivors 修改器", style="App.TLabel").pack(side="left")
        self.locate_btn = ttk.Button(top, text="定位游戏", style="Accent.TButton",
                                     command=self.on_locate_button)
        self.locate_btn.pack(side="right")
        self.status = tk.StringVar(value="进入一局后点“定位游戏”")
        self.status_label = ttk.Label(top, textvariable=self.status, style="AppMuted.TLabel")
        self.status_label.pack(side="right", padx=(0, 12))
        self.bar = ttk.Progressbar(top, mode="indeterminate", length=110)

        # 属性: 角色 and 冒险属性 side by side, as two columns of one card
        stat, stat_head, stat_body = card(root, "属性", "按游戏面板的写法填；勾“锁定”才持续生效")
        stat.grid(row=1, column=0, sticky="nsew", padx=(18, 8), pady=10)
        ttk.Button(stat_head, text="全部解锁", style="Small.TButton",
                   command=self.unlock_all).pack(side="right")
        cols = {}
        for i, group in enumerate(("角色", "冒险属性")):
            f = ttk.Frame(stat_body, style="Body.TFrame")
            f.grid(row=0, column=2 * i, sticky="n")
            ttk.Label(f, text=group, style="Group.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
            ttk.Separator(f).grid(row=1, column=0, columnspan=4, sticky="ew", pady=(2, 6))
            cols[group] = [f, 2]
        ttk.Frame(stat_body, style="Body.TFrame", width=28).grid(row=0, column=1)   # gutter

        right = ttk.Frame(root)
        right.grid(row=1, column=1, sticky="nsew", padx=(8, 18), pady=10)
        state, _h, state_body = card(right, "状态", "写一次就生效")
        state.pack(fill="x")
        prow = 0
        for kind, name, label, _fmt, lockable, unit, group in FIELDS:
            if kind == "player":
                self.add_row(state_body, prow, kind, name, label, unit, lockable)
                prow += 1
            else:                                        # effects sit with the stats
                f, r = cols[group]
                self.add_row(f, r, kind, name, label, unit, lockable)
                cols[group][1] += 1
        hp_line = ttk.Frame(state_body, style="Body.TFrame")
        hp_line.grid(row=prow, column=0, columnspan=5, sticky="w", pady=(10, 0))
        heal = ttk.Button(hp_line, text="回满血", style="Small.TButton", command=self.heal)
        heal.pack(side="left")
        self.full_var = tk.BooleanVar(value=False)      # 锁满血: health kept at 生命值
        full = ttk.Checkbutton(hp_line, text="锁满血", variable=self.full_var, command=self.toggle_full)
        full.pack(side="left", padx=(12, 0))
        self.hp_widgets = [heal, full]

        tb, _h, tb_body = card(right, "极速模式")
        tb.pack(fill="x", pady=(12, 0))
        seg = ttk.Frame(tb_body, style="Body.TFrame")
        seg.pack(side="left")
        self.turbo_var = tk.IntVar(value=1)
        self.turbo_widgets = []
        for speed in turbo.SPEEDS:
            b = ttk.Radiobutton(seg, text="关" if speed == 1 else f"{speed}x", value=speed,
                                variable=self.turbo_var, command=self.set_turbo,
                                style="Seg.Toolbutton", width=4)
            b.pack(side="left", padx=(0, 2))
            self.turbo_widgets.append(b)
        self.turbo_label = ttk.Label(tb_body, text="", width=16, anchor="e", style="Muted.TLabel")
        self.turbo_label.pack(side="right")

        self.save_panel = SavePanel(root, self)
        self.save_panel.frame.grid(row=2, column=0, columnspan=2, sticky="ew",
                                   padx=18, pady=(0, 6))

        bottom = ttk.Frame(root, padding=(18, 0, 18, 12))
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.msg = tk.StringVar()
        self.msg_label = ttk.Label(bottom, textvariable=self.msg, style="Msg.TLabel")
        self.msg_label.pack(side="left")

        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.set_enabled(False)
        root.after(REFRESH_MS, self.refresh)

    # --- layout -------------------------------------------------------------
    def add_row(self, frame, r, kind, name, label, unit, lockable):
        key = (kind, name)
        ttk.Label(frame, text=label).grid(row=r, column=0, sticky="w", padx=(0, 12))
        cur = ttk.Label(frame, text="-", width=8, anchor="e", style="Value.TLabel")
        cur.grid(row=r, column=1, sticky="e", padx=(0, 10))
        entry = ttk.Entry(frame, width=8)
        entry.grid(row=r, column=2, pady=2)
        entry.bind("<Return>", lambda _e: self.on_enter(key))
        lock = tk.BooleanVar(value=False)
        row = {"cur": cur, "entry": entry, "lock": lock, "unit": unit, "widgets": [entry]}
        col = 3
        if kind == "player":
            b = ttk.Button(frame, text="写入", style="Small.TButton", width=5,
                           command=lambda: self.write(key))
            b.grid(row=r, column=col, padx=(6, 0))
            row["widgets"].append(b)
            col += 1
        if lockable:
            c = ttk.Checkbutton(frame, text="锁定", variable=lock,
                                command=lambda: self.toggle_lock(key))
            c.grid(row=r, column=col, padx=(8, 0), sticky="w")
            row["widgets"].append(c)
        self.rows[key] = row

    def set_enabled(self, on):
        for row in self.rows.values():
            for w in row["widgets"]:
                w.state(["!disabled"] if on else ["disabled"])
        for w in self.turbo_widgets + self.hp_widgets:
            w.state(["!disabled"] if on else ["disabled"])

    def connected_status(self):
        self.status.set(f"● 已连接  ·  pid {self.session.pid}")
        self.status_label.configure(style="AppGood.TLabel")

    def set_status(self, text):
        self.status.set(text)
        self.status_label.configure(style="AppMuted.TLabel")

    def say(self, text, error=False):
        self.msg.set(text)
        self.msg_label.configure(style="MsgErr.TLabel" if error else "Msg.TLabel")

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
        self.bar.pack(side="right", padx=(0, 12))
        self.bar.start(12)
        self.set_status(text)

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
        self.bar.pack(side="right", padx=(0, 12))
        self.bar.start(12)
        self.set_status("扫描内存中（约 30～90 秒）…")
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
            self.connected_status()
            self.locate_btn.configure(text="重新定位")
            self.set_enabled(True)
            self.turbo_var.set(1)              # a fresh session: 极速模式 starts off
        elif kind == "relocated":
            old, new = val
            self.misses = 0
            self.connected_status()
            self.say("角色位置没变" if old == new else
                     f"角色在内存里换了位置，已重新连上，锁定保留（{len(self.session.pinned())} 项）")
        elif self.session.base is not None:
            self.disconnect(val)               # relocate failed: start over by hand
        else:
            self.set_status("没有连上")
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

    def set_turbo(self):
        speed = self.turbo_var.get()
        try:
            self.session.set_turbo(speed)
        except SessionError as e:
            self.turbo_var.set(1)
            self.say(str(e), error=True)
            return
        self.say("极速模式 关" if speed == 1 else f"极速模式 {speed}x")

    def show_turbo(self):
        st = self.session.turbo_status()
        text, style = "", "Muted.TLabel"
        if st:
            speed, state, value, note = st
            if speed <= 1 and self.turbo_var.get() > 1:
                self.turbo_var.set(1)          # a new run turned it off
            if state == turbo.ON and value is not None:
                text, style = f"局内 {turbo.fmt_time(value)}", "Locked.TLabel"
            elif state == turbo.LOCATING:
                text = f"{note or '找局内计时'}…"
            elif state == turbo.PAUSED:
                text, style = "计时暂停", "TLabel"
            elif state == turbo.FAILED:
                text, style = "没找到计时，稍后重试", "Err.TLabel"
        self.turbo_label.configure(text=text, style=style)

    def heal(self):
        try:
            self.say(f"血量回满 {self.session.heal():.0f}")
        except SessionError as e:
            self.say(str(e), error=True)

    def toggle_full(self):
        try:
            on = self.session.set_keep_full(self.full_var.get())
            self.say("已锁满血（血量一直等于生命值）" if on else "已解锁满血")
        except SessionError as e:
            self.full_var.set(False)
            self.say(str(e), error=True)

    def unlock_all(self, quiet=False):
        for key, row in self.rows.items():
            if row["lock"].get():
                row["lock"].set(False)
                self.session.pin(*key, None)
        self.full_var.set(False)
        self.session.set_keep_full(False)
        if not quiet:
            self.say("全部解锁")

    # --- refresh loop -------------------------------------------------------
    def refresh(self):
        self.root.after(REFRESH_MS, self.refresh)
        self.save_panel.tick()
        if self.locating or self.session.base is None:
            return
        self.show_turbo()
        vals = None if self.session.lost else self.session.read()
        while self.session.events:
            self.say(self.session.events.pop(0))
            self.connected_status()
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
                                 style="LockedValue.TLabel" if key in pinned else "Value.TLabel")

    def disconnect(self, why=""):
        self.unlock_all(quiet=True)
        self.session.close()
        self.set_enabled(False)
        self.turbo_label.configure(text="", style="Muted.TLabel")
        for row in self.rows.values():
            row["cur"].configure(text="-", style="Value.TLabel")
        self.set_status("连不上角色了（游戏关了或对局结束？）")
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
        self.exe = None                 # exe picked in this window (None: scan_stores.game_exe())
        self.asked_exe = False          # the picker pops up by itself only once

        self.frame, head, f = card(root, "存档", "局外进度 · 游戏完全退出后才能改，每次自动备份")
        self.reload_btn = ttk.Button(head, text="重新读取", style="Small.TButton", command=self.load)
        self.reload_btn.pack(side="right")
        self.exe_btn = ttk.Button(head, text="指定游戏 exe…", style="Small.TButton", command=self.pick_exe)
        self.exe_btn.pack(side="right", padx=(0, 6))
        self.info = tk.StringVar(value="读取中…")
        ttk.Label(f, textvariable=self.info, style="Muted.TLabel").grid(
            row=0, column=0, columnspan=8, sticky="w")

        ttk.Label(f, text="特权重置").grid(row=1, column=0, sticky="w", pady=(10, 0), padx=(0, 12))
        self.resets_cur = ttk.Label(f, text="-", width=10, style="Value.TLabel")
        self.resets_cur.grid(row=1, column=1, sticky="w", pady=(10, 0))
        self.resets_btn = ttk.Button(f, text="清零（恢复全部重置）", style="Small.TButton",
                                     command=self.clear_resets)
        self.resets_btn.grid(row=1, column=2, sticky="w", pady=(10, 0), padx=(0, 40))

        ttk.Label(f, text="印记").grid(row=1, column=3, sticky="w", pady=(10, 0), padx=(0, 12))
        self.cls = ttk.Combobox(f, state="readonly", width=12)
        self.cls.grid(row=1, column=4, sticky="w", pady=(10, 0))
        self.cls.bind("<<ComboboxSelected>>", lambda _e: self.show_marks())
        self.marks_cur = ttk.Label(f, text="-", width=12, anchor="e", style="Value.TLabel")
        self.marks_cur.grid(row=1, column=5, sticky="e", padx=(8, 10), pady=(10, 0))
        self.marks_entry = ttk.Entry(f, width=9)
        self.marks_entry.grid(row=1, column=6, pady=(10, 0))
        self.marks_entry.bind("<Return>", lambda _e: self.write_marks())
        self.marks_btn = ttk.Button(f, text="写入", style="Small.TButton", command=self.write_marks)
        self.marks_btn.grid(row=1, column=7, padx=(6, 0), pady=(10, 0))

        self.hint = ttk.Label(f, text="", style="Muted.TLabel")
        self.hint.grid(row=2, column=0, columnspan=8, sticky="w", pady=(10, 0))
        self.load()

    # --- read -------------------------------------------------------------
    def load(self, exe=None):
        """Read the save in the background. `exe`: one just picked, tried first and
        kept (and remembered for next time) only if its key opens the save."""
        if self.loading:
            return
        self.loading = True
        self.info.set("读取存档中…")
        self.update_state()
        exe = exe or self.exe

        def work():
            try:
                sv = save.Save(exe=exe)
                if exe and exe != self.exe:
                    try:
                        scan_stores.remember_exe(exe)
                    except OSError:
                        pass            # still used for this window
                self.results.put(("ok", (sv, exe)))
            except save.NoExe as e:
                self.results.put(("noexe", str(e)))
            except save.SaveError as e:
                if exe and exe != self.exe:     # just picked: say which file didn't work
                    e = f"用 {exe} 打不开（选的是游戏的 AzerothSurvivors.exe 吗？）：{e}"
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
        if kind == "ok":
            self.sv, exe = val
            if exe:
                self.exe = exe
            self.info.set(f"存档：{self.sv.path}")
        else:
            self.sv = None
            if kind == "noexe":
                self.info.set("读存档要用游戏 exe 里的密钥：点“指定游戏 exe…”选一次 AzerothSurvivors.exe，以后会记住")
            else:
                self.info.set(f"读不了存档：{val}")
        self.show()
        if kind == "noexe" and not self.asked_exe:
            self.asked_exe = True
            self.root.after(100, self.pick_exe)

    def pick_exe(self):
        if self.loading:
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="选择游戏的 AzerothSurvivors.exe（读存档的密钥在里面）",
            filetypes=[("AzerothSurvivors.exe", "AzerothSurvivors.exe"), ("程序", "*.exe")])
        if path:
            self.load(exe=os.path.normpath(path))

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
                                style="Muted.TLabel")

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
    try:            # its own taskbar button (with our icon), not grouped under python.exe
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("azeroth-survivors-trainer.gui")
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
