"""One live connection to the game, shared by the GUI: locate, read, write, lock.

No printing and no sys.exit: failures raise SessionError so a UI can show them.
Locking runs on a background thread; everything else is called from the UI thread.
"""
import math
import struct
import threading
import time

import game
import memscan
import player
import trainer
from offsets import (DOTS_MAX_EXTRA, EFFECT_LIST, EFFECT_SOURCES, ITEM_DEF_KIND, ITEM_LIST,
                     ITEM_STRIDE, ITEM_VALUE, PLAYER, PLAYER_STAT_BASE, PLAYER_UNVERIFIED,
                     STAT, STAT_READONLY, STAT_TAIL)

# (kind, name, label, struct format, lockable, unit, group)
#   kind  stat   — the f32 cache the game recomputes every 1-3 s: only a lock sticks
#         player — real run state: a single write sticks until the game changes it
#         effect — summed from the gear/card effect list each time (荆棘): we
#                  change the first entry of its tag so the sum is the value
#   label, order and group follow the game's own character pane (角色 box, then
#   冒险属性), so a row here reads like the line it changes there.
#   unit  how the pane shows the raw value; the panels show and take input that way:
#     num    as is (生命值 447, 回复 3.3, 拾取 31码)       int   whole number
#     +int   whole number with a sign (目标数 +5)
#     %      v * 100        (暴击伤害 225%, 护甲 32%, 回响 50%)
#     %.1    v * 100, one decimal (暴击 26.4%)
#     +%     v * 100, signed (幸运 +1000%, 增益强度 +44%, 持续伤害急速 +77%)
#     +%1    (v - 1) * 100  (伤害 +73%, 范围 +15%, 速度 +32%, 经验值 / 金币 +0%)
#     haste  (1 - v) * 100  (急速 +12%: the stat is a cooldown multiplier)
#     dots   min(v, 3) + 1  (持续伤害层数: the stat holds the extra stacks)
FIELDS = [
    ("stat", "hp", "生命值", "<f", True, "num", "角色"),
    ("stat", "dmg", "伤害", "<f", True, "+%1", "角色"),
    ("stat", "cd", "急速", "<f", True, "haste", "角色"),
    ("stat", "crit", "暴击", "<f", True, "%.1", "角色"),
    ("stat", "cm", "暴击伤害", "<f", True, "%", "角色"),
    ("stat", "arm", "护甲", "<f", True, "%", "角色"),
    ("stat", "area", "范围", "<f", True, "+%1", "角色"),
    ("stat", "targets", "目标数", "<i", True, "+int", "角色"),
    ("stat", "echo", "回响", "<f", True, "%", "角色"),
    ("stat", "spd", "速度", "<f", True, "+%1", "角色"),
    ("stat", "reg", "回复", "<f", True, "num", "角色"),
    ("stat", "pick", "拾取（码）", "<f", True, "num", "角色"),
    ("effect", "lifesteal", "生命偷取（装备部分）", "<f", True, "%.1", "冒险属性"),
    ("stat", "heal", "击杀治疗", "<f", True, "num", "冒险属性"),
    ("effect", "thorns", "荆棘", "<f", True, "%", "冒险属性"),
    ("stat", "lives", "生命数", "<i", True, "int", "冒险属性"),
    ("stat", "xp", "经验值", "<f", True, "+%1", "冒险属性"),
    ("stat", "gold", "金币", "<f", True, "+%1", "冒险属性"),
    ("stat", "luck", "幸运", "<f", True, "+%", "冒险属性"),
    ("stat", "bp", "增益强度", "<f", True, "+%", "冒险属性"),
    ("stat", "dots", "持续伤害层数", "<i", True, "dots", "冒险属性"),
    ("stat", "doth", "持续伤害急速", "<f", True, "+%", "冒险属性"),
    ("player", "gold", "金币数量", "<i", True, "int", "状态"),
    ("player", "hp", "当前血量", "<f", True, "num", "状态"),
    ("player", "level", "等级", "<i", False, "int", "状态"),    # locking these loops level-ups
    ("player", "xp", "当前经验", "<i", False, "int", "状态"),
    ("player", "kills", "击杀", "<i", False, "int", "状态"),
    ("player", "rerolls", "重抽次数", "<i", True, "int", "状态"),
    ("player", "banishes", "放逐次数", "<i", True, "int", "状态"),
    ("player", "chest", "宝箱价格", "<i", True, "int", "状态"),
]
STAT_ALL = {**STAT, **STAT_TAIL}
assert all(n in STAT_ALL and n not in STAT_READONLY for k, n, *_ in FIELDS if k == "stat")
assert all(n in PLAYER for k, n, *_ in FIELDS if k == "player")
assert all(n in EFFECT_SOURCES for k, n, *_ in FIELDS if k == "effect")
SPEC = {(k, n): (lbl, fmt, lock) for k, n, lbl, fmt, lock, *_ in FIELDS}
UNIT = {(k, n): unit for k, n, _l, _f, _c, unit, _g in FIELDS}


# --- game units <-> raw values (mirrored in addon/AZSRemote/AZSRemote.lua) -----
def to_game(unit, v):
    """Raw value -> the number the game's pane shows."""
    if unit == "dots":
        return min(max(v, 0), DOTS_MAX_EXTRA) + 1
    if unit in ("%", "%.1", "+%"):
        return v * 100.0
    if unit == "+%1":
        return (v - 1.0) * 100.0
    if unit == "haste":
        return (1.0 - v) * 100.0
    return v


def from_game(unit, x):
    """A number typed the way the pane shows it -> the raw value to write."""
    if unit == "dots":
        return float(min(max(x - 1, 0), DOTS_MAX_EXTRA))
    if unit in ("%", "%.1", "+%"):
        return x / 100.0
    if unit == "+%1":
        return 1.0 + x / 100.0
    if unit == "haste":
        return 1.0 - x / 100.0
    return x


def show(unit, v):
    """Raw value -> text like the pane's: '+73%', '26.4%', '+5', '447'."""
    x = to_game(unit, v)
    r = math.floor(x + 0.5)              # like the Lua side, not banker's rounding
    if unit == "%.1":                    # the pane gives crit one decimal: 26.4%
        s = "%.1f" % x
    elif unit == "num" and abs(x - r) >= 0.05:
        s = "%.1f" % x                   # 回复 3.3
    else:
        s = "%d" % r                     # every other percentage is whole: +12%, 32%
    if unit in ("+%", "+%1", "haste", "+int") and x >= 0 and not s.startswith("-"):
        s = "+" + s
    return s + ("%" if "%" in unit or unit == "haste" else "")


def parse(unit, text):
    """Input as the pane writes it ('+50%', '50', '-10%') -> raw value."""
    t = str(text).strip().replace("%", "").replace("+", "").replace("＋", "").replace("％", "")
    return from_game(unit, float(t))
FOLLOW_EVERY = 1.0      # seconds between "is ours the one that moves?" checks


class SessionError(Exception):
    pass


def offset(kind, name):
    """Byte offset of a field from the player-object base (not for effects)."""
    if kind == "effect":
        raise ValueError(f"{name} has no fixed offset: it lives in the effect list")
    return PLAYER_STAT_BASE + STAT_ALL[name] if kind == "stat" else PLAYER[name][0]


def coerce(kind, name, value):
    """User input -> number of the field's type; raises ValueError."""
    if (kind, name) not in SPEC:
        raise ValueError(f"unknown field {kind}.{name}")
    if kind == "player" and name in PLAYER_UNVERIFIED:
        raise ValueError(f"{name}: offset not verified on this game version")
    fmt = SPEC[(kind, name)][1]
    v = float(str(value).strip())
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError("not a finite number")
    if fmt == "<i":
        if v != int(v):
            raise ValueError("needs a whole number")
        v = int(v)
        if not -2**31 <= v < 2**31:
            raise ValueError("out of range")
    return v


class Session:
    def __init__(self, interval=trainer.PIN_INTERVAL):
        self.interval = interval
        self.pid = self.h = self.base = None
        self._pins = {}                 # (kind, name) -> packed bytes
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.lost = False               # set by the lock thread when the target goes away
        self.alternates = []            # tied look-alikes from locate()
        self._sigs, self._next_check = {}, 0.0
        self.events = []                # human-readable notes for the UI / agent log
        self.lost_reason = None         # why the lock thread gave up, for the log
        self._last_vals = None          # last good read(): the hint for relocate()

    # --- connect ------------------------------------------------------------
    def locate(self):
        """Find the game and the live player object (two memory sweeps, 30-90 s)."""
        self.close()
        pid = game.find_pid()
        if not pid:
            raise SessionError("没找到游戏进程，先启动游戏并进入一局")
        try:
            h = memscan.open_process(pid, write=True)
        except OSError as e:
            raise SessionError(f"打不开游戏进程 {pid}：{e}") from None
        try:
            top, _hud = player.locate_ranked(h)
            base = (player.pick_live(h, top) if len(top) > 1 else None) or top[0]
        except SystemExit as e:        # player.locate reports failure by exiting
            memscan.k32.CloseHandle(h)
            raise SessionError(str(e.code)) from None
        except BaseException:
            memscan.k32.CloseHandle(h)
            raise
        self.pid, self.h, self.base, self.lost = pid, h, base, False
        self.lost_reason, self._last_vals = None, None
        # look-alikes that tied with it: if ours turns out frozen and one of these
        # starts moving (we located on 移动以开始, say), read() switches over
        self.alternates = [a for a in top if a != base]
        self._sigs, self._next_check = {}, 0.0
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return base

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self._thread = None
        with self._lock:
            self._pins.clear()
        if self.h:
            memscan.k32.CloseHandle(self.h)
        self.pid = self.h = self.base = None

    @property
    def connected(self):
        return self.base is not None and not self.lost

    # --- read / write -------------------------------------------------------
    def _checks(self):
        with self._lock:
            pinned = {n for k, n in self._pins if k == "stat"}
        return trainer.stat_checks(None, skip=pinned)

    def alive(self, blob=None):
        """Does the object still carry a plausible stat block?"""
        if blob is None:
            blob = memscan.read(self.h, self.base + PLAYER_STAT_BASE, trainer.SPAN * 4)
            if not blob or len(blob) < trainer.SPAN * 4:
                return False
        else:
            blob = blob[PLAYER_STAT_BASE:PLAYER_STAT_BASE + trainer.SPAN * 4]
        return trainer.block_ok(memscan.as_f32(blob), self._checks())

    def read(self):
        """{(kind, name): value} for every field, or None if the target is gone."""
        if not self.connected:
            return None
        self._follow_live()
        blob = memscan.read(self.h, self.base, player.OBJ_SIZE)
        if not blob or len(blob) < player.OBJ_SIZE or not self.alive(blob):
            return None
        vals = {(k, n): (self._effect_total(n) if k == "effect"
                         else struct.unpack_from(fmt, blob, offset(k, n))[0])
                for (k, n), (_l, fmt, _c) in SPEC.items()}
        self._last_vals = vals
        return vals

    # --- summed stats (荆棘, 生命偷取): the game adds list entries each time -----
    def _vec(self, field_offsets, stride):
        """(pointer, raw bytes) of a Vec whose ptr/len live in the player object."""
        raw = memscan.read(self.h, self.base + field_offsets[0], 8)
        rawn = memscan.read(self.h, self.base + field_offsets[1], 8)
        if not raw or not rawn:
            return 0, b""
        ptr, n = struct.unpack("<Q", raw)[0], struct.unpack("<Q", rawn)[0]
        if not ptr or not 0 < n < 1024:
            return 0, b""
        data = memscan.read(self.h, ptr, stride * n)
        return (ptr, data) if data and len(data) == stride * n else (0, b"")

    def _effect_entries(self, name):
        """[(address of the f32 value, value)] for every list entry that feeds name."""
        out = []
        for source, tag in EFFECT_SOURCES[name]:
            if source == "effect":
                ptr, data = self._vec(EFFECT_LIST, 8)
                out += [(ptr + 8 * i + 4, struct.unpack_from("<f", data, 8 * i + 4)[0])
                        for i in range(len(data) // 8) if data[8 * i] == tag]
            else:                                   # items: kind sits in the definition
                ptr, data = self._vec(ITEM_LIST, ITEM_STRIDE)
                for i in range(len(data) // ITEM_STRIDE):
                    e = ITEM_STRIDE * i
                    kind = memscan.read(self.h, struct.unpack_from("<Q", data, e)[0] + ITEM_DEF_KIND, 1)
                    if kind and kind[0] == tag:
                        out.append((ptr + e + ITEM_VALUE,
                                    struct.unpack_from("<f", data, e + ITEM_VALUE)[0]))
        return out

    def _effect_total(self, name):
        return sum(v for _a, v in self._effect_entries(name))

    def _write_effect(self, name, value):
        entries = self._effect_entries(name)
        if not entries:
            label = SPEC[("effect", name)][0]
            raise SessionError(f"{label}：现在没有任何来源可改，先拿到一件带{label}的装备或卡")
        rest = sum(v for _a, v in entries[1:])
        if not memscan.write(self.h, entries[0][0], struct.pack("<f", value - rest)):
            raise SessionError(f"写入 {name} 失败（游戏已退出？）")

    def _follow_live(self):
        """Switch to a tied look-alike if ours stays still while that one moves."""
        if not self.alternates or time.monotonic() < self._next_check:
            return
        self._next_check = time.monotonic() + FOLLOW_EVERY
        sigs = {a: player.signature(self.h, a) for a in [self.base] + self.alternates}
        old, self._sigs = self._sigs, sigs
        if not old or old.get(self.base) != sigs[self.base]:
            return                          # first look, or ours is the one moving
        for a in self.alternates:
            if a in old and old[a] != sigs[a]:
                self.alternates = [x for x in self.alternates if x != a] + [self.base]
                self.events.append(f"switched to 0x{a:X}: 0x{self.base:X} stayed frozen")
                self.base = a
                self._sigs = {}
                return

    def write(self, kind, name, value):
        if not self.connected:
            raise SessionError("还没定位到游戏")
        v = coerce(kind, name, value)
        if kind == "effect":
            self._write_effect(name, v)
        elif not memscan.write(self.h, self.base + offset(kind, name),
                               struct.pack(SPEC[(kind, name)][1], v)):
            raise SessionError(f"写入 {name} 失败（游戏已退出？）")
        return v

    def heal(self):
        if not self.connected:
            raise SessionError("还没定位到游戏")
        raw = memscan.read(self.h, self.base + player.MAXHP, 4)
        if not raw:
            raise SessionError("读不到最大生命（游戏已退出？）")
        mx = struct.unpack("<f", raw)[0]
        return self.write("player", "hp", mx)

    # --- lock ---------------------------------------------------------------
    def pin(self, kind, name, value):
        """Keep re-writing a field; value None releases it."""
        if value is None:
            with self._lock:
                self._pins.pop((kind, name), None)
            return None
        if not SPEC[(kind, name)][2]:
            raise ValueError(f"{name} cannot be locked")
        v = coerce(kind, name, value)
        with self._lock:
            self._pins[(kind, name)] = struct.pack(SPEC[(kind, name)][1], v)
        return v

    def pinned(self):
        with self._lock:
            return set(self._pins)

    def tick(self, misses=0):
        """One pass: is the hero still there? then re-apply the locks.
        Returns the new consecutive-miss count."""
        if not self.alive():
            return misses + 1
        with self._lock:
            pins = list(self._pins.items())
        for (k, n), data in pins:
            if k == "effect":
                try:
                    self._write_effect(n, struct.unpack("<f", data)[0])
                except SessionError:
                    pass                    # the source went away (gear swapped): skip
            else:
                memscan.write(self.h, self.base + offset(k, n), data)
        return 0

    def _run(self):
        misses = 0
        while not self._stop.wait(self.interval):
            misses = self.tick(misses)
            if misses >= trainer.MAX_MISSES:
                # The game moves the hero (Bevy relocates an entity's data when
                # its components change) or the run ended. Locks are kept for
                # relocate(); nothing is written meanwhile.
                self.lost_reason = self._why_dead()
                self.lost = True
                return

    def _why_dead(self):
        raw = memscan.read(self.h, self.base + PLAYER_STAT_BASE, trainer.SPAN * 4)
        if not raw:
            return f"0x{self.base:X} is no longer readable"
        lvl = memscan.read(self.h, self.base + PLAYER["level"][0], 4)
        lvl = struct.unpack("<i", lvl)[0] if lvl else None
        return f"0x{self.base:X} no longer holds a stat block (level reads {lvl})"

    # --- find the hero again --------------------------------------------------
    def _last_known(self):
        """The hero's numbers from our last good read, shaped like a HUD frame."""
        v = self._last_vals
        if not v:
            return None
        return {"level": v[("player", "level")], "kills": v[("player", "kills")],
                "gold": v[("player", "gold")], "hpMax": v[("stat", "hp")]}

    def relocate(self):
        """Find the hero again after it moved, keeping every lock.

        Scores candidates against the last values we read (one sweep, ~half the
        time of locate()); falls back to the HUD if that finds nothing, e.g. the
        run ended and the numbers started over. Returns (old base, new base).
        """
        if self.h is None:
            raise SessionError("还没定位过，先点“定位游戏”")
        top = None
        hint = self._last_known()
        try:
            if hint:
                try:
                    top, _ = player.locate_ranked(self.h, hint)
                except SystemExit:
                    top = None
            if not top:
                top, _ = player.locate_ranked(self.h)
        except SystemExit as e:
            raise SessionError(str(e.code)) from None
        base = (player.pick_live(self.h, top) if len(top) > 1 else None) or top[0]
        old, self.base = self.base, base
        self.alternates = [a for a in top if a != base]
        self._sigs, self._next_check = {}, 0.0
        self.lost, self.lost_reason = False, None
        if not (self._thread and self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return old, base
