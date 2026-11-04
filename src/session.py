"""One live connection to the game, shared by the GUI: locate, read, write, lock.

No printing and no sys.exit: failures raise SessionError so a UI can show them.
Locking runs on a background thread; everything else is called from the UI thread.
"""
import struct
import threading

import game
import memscan
import player
import trainer
from offsets import PLAYER, PLAYER_STAT_BASE, PLAYER_UNVERIFIED, STAT, STAT_READONLY

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
    ("player", "gold", "金币", "<i", True),
    ("player", "hp", "当前血量", "<f", True),
    ("player", "level", "等级", "<i", False),    # locking these loops level-ups
    ("player", "xp", "当前经验", "<i", False),
    ("player", "kills", "击杀", "<i", False),
    ("player", "rerolls", "重抽次数", "<i", True),
    ("player", "banishes", "放逐次数", "<i", True),
    ("player", "chest", "宝箱价格", "<i", True),
]
assert all(n in STAT and n not in STAT_READONLY for k, n, *_ in FIELDS if k == "stat")
assert all(n in PLAYER for k, n, *_ in FIELDS if k == "player")
SPEC = {(k, n): (lbl, fmt, lock) for k, n, lbl, fmt, lock in FIELDS}


class SessionError(Exception):
    pass


def offset(kind, name):
    """Byte offset of a field from the player-object base."""
    return PLAYER_STAT_BASE + STAT[name] if kind == "stat" else PLAYER[name][0]


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
            base, _hud = player.locate(h)
        except SystemExit as e:        # player.locate reports failure by exiting
            memscan.k32.CloseHandle(h)
            raise SessionError(str(e.code)) from None
        except BaseException:
            memscan.k32.CloseHandle(h)
            raise
        self.pid, self.h, self.base, self.lost = pid, h, base, False
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
        blob = memscan.read(self.h, self.base, player.OBJ_SIZE)
        if not blob or len(blob) < player.OBJ_SIZE or not self.alive(blob):
            return None
        return {(k, n): struct.unpack_from(fmt, blob, offset(k, n))[0]
                for (k, n), (_l, fmt, _c) in SPEC.items()}

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
        """One lock pass; returns the new consecutive-miss count."""
        with self._lock:
            pins = list(self._pins.items())
        if not pins:
            return 0
        if not self.alive():
            return misses + 1
        for (k, n), data in pins:
            memscan.write(self.h, self.base + offset(k, n), data)
        return 0

    def _run(self):
        misses = 0
        while not self._stop.wait(self.interval):
            misses = self.tick(misses)
            if misses >= trainer.MAX_MISSES:
                self.lost = True
                with self._lock:
                    self._pins.clear()
                return
