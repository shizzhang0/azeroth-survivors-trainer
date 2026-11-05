"""One live connection to the game, shared by the GUI: locate, read, write, lock.

No printing and no sys.exit: failures raise SessionError so a UI can show them.
Locking runs on a background thread; everything else is called from the UI thread.
"""
import struct
import threading
import time

import game
import memscan
import player
import trainer
from offsets import PLAYER, PLAYER_STAT_BASE, PLAYER_UNVERIFIED, STAT, STAT_READONLY, STAT_TAIL

# (kind, name, label, struct format, lockable)
#   stat   — the f32 cache the game recomputes every 1-3 s: only a lock sticks
#   player — real run state: a single write sticks until the game changes it
FIELDS = [
    ("stat", "hp", "最大生命", "<f", True),
    ("stat", "dmg", "伤害倍率", "<f", True),
    ("stat", "cd", "冷却倍率（越小越快）", "<f", True),
    ("stat", "crit", "暴击率", "<f", True),
    ("stat", "cm", "暴击伤害倍率", "<f", True),
    ("stat", "spd", "移动速度", "<f", True),
    ("stat", "pick", "拾取半径", "<f", True),
    ("stat", "xp", "经验倍率", "<f", True),
    ("stat", "arm", "减伤", "<f", True),
    ("stat", "reg", "每秒回血", "<f", True),
    ("stat", "area", "范围倍率", "<f", True),
    ("stat", "luck", "幸运", "<f", True),
    ("stat", "gold", "金币倍率", "<f", True),
    ("stat", "targets", "额外目标数", "<i", True),
    ("stat", "echo", "回响", "<f", True),
    ("stat", "heal", "击杀治疗", "<f", True),
    ("stat", "bp", "增益强度", "<f", True),
    ("stat", "doth", "持续伤害急速", "<f", True),
    ("player", "gold", "金币", "<i", True),
    ("player", "hp", "当前血量", "<f", True),
    ("player", "level", "等级", "<i", False),    # locking these loops level-ups
    ("player", "xp", "当前经验", "<i", False),
    ("player", "kills", "击杀", "<i", False),
    ("player", "rerolls", "重抽次数", "<i", True),
    ("player", "banishes", "放逐次数", "<i", True),
    ("player", "chest", "宝箱价格", "<i", True),
]
STAT_ALL = {**STAT, **STAT_TAIL}
assert all(n in STAT_ALL and n not in STAT_READONLY for k, n, *_ in FIELDS if k == "stat")
assert all(n in PLAYER for k, n, *_ in FIELDS if k == "player")
SPEC = {(k, n): (lbl, fmt, lock) for k, n, lbl, fmt, lock in FIELDS}
FOLLOW_EVERY = 1.0      # seconds between "is ours the one that moves?" checks


class SessionError(Exception):
    pass


def offset(kind, name):
    """Byte offset of a field from the player-object base."""
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
        vals = {(k, n): struct.unpack_from(fmt, blob, offset(k, n))[0]
                for (k, n), (_l, fmt, _c) in SPEC.items()}
        self._last_vals = vals
        return vals

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
        if not memscan.write(self.h, self.base + offset(kind, name),
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
