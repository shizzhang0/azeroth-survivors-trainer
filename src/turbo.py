"""极速模式: the run timer runs at N x, nothing else does.

The run timer is the HUD's "time", and boss arrivals (8:00, 14:00) are scheduled on
it. Combat, cooldowns and movement keep their own speed (Bevy's virtual clock would
speed all of that up too: docs/OFFSETS.md). v1127: at 4x the 8:00
boss came after 2 real minutes.

Where the timer is: v1127 keeps it in the player object (offsets.RUN_TIME), so a
Turbo given the hero's address uses that at once. If it doesn't look like the timer
there (another game version), it is searched for, in memory only:
  1. read the HUD run time;
  2. sweep memory for f32 / f64 / Duration slots about that value (a Duration is
     u64 seconds + u32 nanoseconds, what a Bevy Stopwatch holds), sweep again per
     chunk and keep those that grew with wall time, then recheck them one by one;
  3. dozens of copies follow the timer (v1127: 54). Add a couple of seconds to each
     candidate alone and take it back off: the master keeps the write and drags the
     copies along.

Speeding it up: every TICK, the game's own growth since our last write is scaled by
the factor. Card picks and pauses stop the timer, so nothing is added then. A new run
starts the timer from 0 again: then 极速模式 turns itself off (each run starts at 1x).
"""
import struct
import threading
import time

import numpy as np

import game
import memscan
from offsets import RUN_TIME

SLACK = 1.5          # s: the HUD frame lags the timer by up to an update
LIVE_WAIT = 1.0
TICK = 0.05          # s between speed-loop steps
KINDS = ("f32", "f64", "dur")
NANOS = 1_000_000_000
SIZE = {"f32": 4, "f64": 8, "dur": 12}
MAX_TIME = 86400.0
PROBE_ADD = 2.0      # s added (and taken back) to tell the master from its copies
MAX_CANDIDATES = 400
SPEEDS = (1, 2, 4, 8)          # what the panels offer; 1 = off
MAX_SPEED = 20.0
RETRY = 15.0         # s between attempts to find the timer while 极速模式 is on
STALE = 60.0         # s the timer may stand still before we ask the HUD whether it moved on
NEW_RUN_DROP = 5.0   # s the timer falls back by when a new run starts it from 0 again

# Turbo.state
OFF, LOCATING, ON, PAUSED, FAILED = "off", "locating", "on", "paused", "failed"


class TurboError(Exception):
    pass


# ---- pure parts (tested offline) -------------------------------------------------

def range_hits(raw, lo, hi):
    """{kind: (byte offsets, values)} of every slot in raw whose value is in [lo, hi]."""
    out = {}
    n4, n8 = len(raw) // 4, len(raw) // 8
    f = np.frombuffer(raw, dtype="<f4", count=n4)
    with np.errstate(invalid="ignore"):
        i = np.flatnonzero((f >= lo) & (f <= hi))
    out["f32"] = (i * 4, f[i].astype(np.float64))
    d = np.frombuffer(raw, dtype="<f8", count=n8)
    with np.errstate(invalid="ignore"):
        i = np.flatnonzero((d >= lo) & (d <= hi))
    out["f64"] = (i * 8, d[i])
    q = np.frombuffer(raw, dtype="<u8", count=n8)
    if len(q) >= 2:
        secs, nanos = q[:-1], q[1:] & np.uint64(0xFFFF_FFFF)
        i = np.flatnonzero((secs >= np.uint64(max(0, int(lo))))
                           & (secs <= np.uint64(int(hi))) & (nanos < np.uint64(NANOS)))
        v = secs[i].astype(np.float64) + nanos[i].astype(np.float64) / NANOS
        keep = (v >= lo) & (v <= hi)
        out["dur"] = (i[keep] * 8, v[keep])
    else:
        out["dur"] = (np.empty(0, dtype=np.intp), np.empty(0))
    return out


def values_at(raw, kind, offs):
    """Values of `kind` at byte offsets offs (aligned for their kind) in raw."""
    if kind == "f32":
        return np.frombuffer(raw, dtype="<f4", count=len(raw) // 4)[offs // 4].astype(np.float64)
    if kind == "f64":
        return np.frombuffer(raw, dtype="<f8", count=len(raw) // 8)[offs // 8]
    q = np.frombuffer(raw, dtype="<u8", count=len(raw) // 8)
    nanos = (q[offs // 8 + 1] & np.uint64(0xFFFF_FFFF)).astype(np.float64)
    return q[offs // 8].astype(np.float64) + nanos / NANOS


def still_growing(v0, v1, dt):
    """Slots that grew like a run timer over dt wall seconds. A card pick or pause
    stops the timer for part of the gap, so as little as 30% of dt is accepted."""
    with np.errstate(invalid="ignore"):
        g = np.asarray(v1, dtype=np.float64) - np.asarray(v0, dtype=np.float64)
        return (g >= 0.3 * dt) & (g <= dt + 0.3)


def decode(kind, raw):
    if raw is None:
        return None
    if kind == "f32" and len(raw) >= 4:
        v = float(struct.unpack_from("<f", raw)[0])
    elif kind == "f64" and len(raw) >= 8:
        v = struct.unpack_from("<d", raw)[0]
    elif kind == "dur" and len(raw) >= 12:
        secs, nanos = struct.unpack_from("<QI", raw)
        if nanos >= NANOS:
            return None
        v = secs + nanos / NANOS
    else:
        return None
    return v if v == v else None            # NaN: not a time


def encode(kind, value):
    if kind == "f32":
        return struct.pack("<f", value)
    if kind == "f64":
        return struct.pack("<d", value)
    secs = int(value)
    return struct.pack("<QI", secs, min(NANOS - 1, int(round((value - secs) * NANOS))))


def scaled(t, last, speed, max_step=0.5, min_step=1e-3):
    """One speed-loop step. t: the timer now; last: what we left it at.
    Returns (value to write or None, new last). The game's own growth since our last
    write is scaled by speed; no growth (card pick, pause) adds nothing, and a jump
    (new run, someone else's write) is taken as is. Changes under min_step are f32
    rounding of our own write, not the game moving."""
    grown = t - last
    if min_step < grown <= max_step:
        new = t + (speed - 1) * grown
        return new, new
    return None, t


def pick_master(results):
    """results: [(candidate, stuck, how many jumped)] from probe().
    The master keeps the write and drags the most copies along; None if none stuck."""
    stuck = [(jumped, c) for c, ok, jumped in results if ok]
    return max(stuck, key=lambda s: s[0])[1] if stuck else None


def fmt_time(t):
    """Run time as the HUD shows it: 7:28."""
    t = max(0.0, t)
    return f"{int(t // 60)}:{int(t % 60):02d}"


# ---- process side -------------------------------------------------------------

def read_val(h, addr, kind):
    return decode(kind, memscan.read(h, addr, SIZE[kind]))


def write_val(h, addr, kind, value):
    return memscan.write(h, addr, encode(kind, value))


class RangeScan:
    """memscan.sweep visitor: slots holding about the run time when their chunk was read.
    expected(now) gives the [lo, hi] run time can be in at wall time now."""

    def __init__(self, expected):
        self.expected = expected
        self.chunks = []        # (chunk addr, {kind: (offsets, values)}, read at)

    def visitor(self):
        return 16, self

    def __call__(self, addr, raw, end):
        now = time.perf_counter()
        lo, hi = self.expected(now)
        hits = {k: v for k, v in range_hits(raw[:end], lo, hi).items() if len(v[0])}
        if hits:
            self.chunks.append((addr, hits, now))


def regrown(h, chunks):
    """Second look at every chunk with hits, one read per chunk: (addr, kind) of the
    slots that grew like the run timer since the first look."""
    out = []
    for addr, hits, stamp in chunks:
        span = max(int(offs.max()) for offs, _v in hits.values()) + 16
        raw = memscan.read(h, addr, span)
        now = time.perf_counter()
        if not raw or len(raw) < span:
            continue
        for kind, (offs, v0) in hits.items():
            keep = still_growing(v0, values_at(raw, kind, offs), now - stamp)
            out.extend((addr + int(o), kind) for o in offs[keep])
    return out


def hud_time(h):
    """(HUD run time, wall time) of the current run, or None."""
    frame = game.read_hud(h, require=("time",))
    return (frame["time"], time.perf_counter()) if frame else None


def find_live(h, say=lambda _msg: None):
    """(addr, kind) of every slot that reads about the HUD run time and grows with it."""
    say("读 HUD 局内时间")
    hud = hud_time(h)
    if not hud:
        raise TurboError("没读到局内时间：是不是不在一局里？")
    t0, w0 = hud
    say(f"HUD 局内时间 {t0:.1f} 秒，扫描内存")
    scan = RangeScan(lambda now: (t0 - SLACK, t0 + (now - w0) + SLACK))
    memscan.sweep(h, [scan.visitor()])
    hits = sorted(set(regrown(h, scan.chunks)))
    say(f"还在走的 {len(hits)} 个，逐个复查")
    # one more look a second apart, read by read: drops whatever grew by chance
    r0 = [(read_val(h, a, k), time.perf_counter()) for a, k in hits]
    time.sleep(LIVE_WAIT)
    live = []
    for (a, k), (v0, s0) in zip(hits, r0):
        v1, s1 = read_val(h, a, k), time.perf_counter()
        if v0 is not None and v1 is not None and still_growing(v0, v1, s1 - s0):
            live.append((a, k))
    if not live:
        raise TurboError("局内计时没在走（选卡、暂停或菜单中？）")
    return live


def probe(h, live, add=PROBE_ADD, early=True, stop=None):
    """Add `add` s to each candidate alone, see which keep it and which others follow,
    take it back off. Returns [(candidate, stuck, how many jumped)]. With `early`, stops
    at the first one that keeps it and drags at least a third of the rest along."""
    results = []
    for c in live:
        if stop is not None and stop.is_set():
            break                                   # closing: no more writes
        before = {x: read_val(h, *x) for x in live}
        if before[c] is None:
            continue
        w0 = time.perf_counter()
        write_val(h, *c, before[c] + add)
        time.sleep(0.3)
        after = {x: read_val(h, *x) for x in live}
        grown = time.perf_counter() - w0            # what the timer does on its own
        jumped = sum(1 for x in live if None not in (before[x], after[x])
                     and after[x] - before[x] >= add / 2 + min(grown, 1.0))
        stuck = after[c] is not None and after[c] - before[c] >= add / 2 + min(grown, 1.0)
        if stuck:
            write_val(h, *c, read_val(h, *c) - add)        # undo
        results.append((c, stuck, jumped))
        if early and stuck and jumped >= max(3, len(live) // 3):
            break
        time.sleep(0.1)
    return results


def locate(h, say=lambda _msg: None, stop=None):
    """(addr, kind) of the run timer. Raises TurboError. Takes about a minute."""
    live = find_live(h, say)
    if stop is not None and stop.is_set():
        raise TurboError("已停止")
    if len(live) > MAX_CANDIDATES:
        raise TurboError(f"候选太多（{len(live)} 个），没法试")
    say(f"试 {len(live)} 个候选，找真正的计时")
    master = pick_master(probe(h, live, stop=stop))
    if not master:
        raise TurboError("找不到真正的计时（候选都改不动）")
    return master


def verify(h, c, wait=LIVE_WAIT):
    """Is c a slot that reads like a run time and is moving right now?"""
    v0, s0 = read_val(h, *c), time.perf_counter()
    if v0 is None or not 0 <= v0 < MAX_TIME:
        return False
    time.sleep(wait)
    v1, s1 = read_val(h, *c), time.perf_counter()
    return v1 is not None and bool(still_growing(v0, v1, s1 - s0))


_known = {}          # pid -> (addr, kind): the timer found in that game process


class Turbo:
    """极速模式 for one game process: a thread that finds the run timer when asked to
    and then runs it at `speed` x. set_speed(1) turns it off (time already added stays).

    where(): the timer's address from the hero's (player + offsets.RUN_TIME), or None
    while the hero isn't known. Used first and followed when the hero moves; if it
    turns out not to be the timer, the memory search takes over for good.

    state: OFF, LOCATING, ON, PAUSED (timer standing still: card pick, menu, death
    screen) or FAILED (note says why; it tries again every RETRY seconds).
    """

    def __init__(self, h, pid, where=None):
        self.h, self.pid = h, pid
        self.where = where
        self._use_offset = where is not None
        self._from_offset = False           # is self.timer the offset's address?
        self.speed = 1.0
        self.timer = _known.get(pid)        # (addr, kind) or None
        self.state, self.note, self.value = OFF, "", None
        self.events = []                    # notes for the UI / agent log
        self._recheck = self.timer is not None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_speed(self, speed):
        speed = float(speed)
        if not 1 <= speed <= MAX_SPEED:
            raise ValueError(f"极速模式倍率要在 1～{MAX_SPEED:g} 之间")
        self.speed = speed
        self._wake.set()
        return speed

    def recheck(self):
        """A new run (or the hero moved): make sure the timer is still where it was."""
        self._recheck = True
        self._wake.set()

    def close(self):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2)

    # --- thread ---------------------------------------------------------------
    def _sleep(self, secs):
        self._wake.wait(secs)
        self._wake.clear()
        return not self._stop.is_set()

    def _at_offset(self):
        """(addr, "f32") at the hero's timer offset if it reads like a run time."""
        addr = self.where() if self._use_offset else None
        if addr is None:
            return None
        v = read_val(self.h, addr, "f32")
        return (addr, "f32") if v is not None and 0 <= v < MAX_TIME else None

    def _find(self):
        """Make sure self.timer is the live run timer; False if not found."""
        at = self._at_offset()
        if at:
            self.timer, self._from_offset, self._recheck = at, True, False
            return True
        self._from_offset = False
        if self.timer and (not self._recheck or verify(self.h, self.timer)):
            self._recheck = False
            return True
        cached = _known.get(self.pid)
        if cached and cached != self.timer and verify(self.h, cached):
            self.timer, self._recheck = cached, False
            return True
        self.state, self.note = LOCATING, "找局内计时"
        try:
            self.timer = locate(self.h, say=self._progress, stop=self._stop)
        except TurboError as e:
            self.timer, self.state, self.note = None, FAILED, str(e)
            self.events.append(f"极速模式：{e}，{RETRY:.0f} 秒后再试")
            return False
        except OSError as e:
            self.timer, self.state, self.note = None, FAILED, f"读写失败：{e}"
            return False
        _known[self.pid] = self.timer
        self._recheck = False
        self.events.append(f"极速模式：局内计时在 0x{self.timer[0]:X}（{self.timer[1]}）")
        return True

    def _progress(self, msg):
        self.note = msg

    def _new_run(self, t):
        """The timer started over: a new run. Each run starts at 1x."""
        self.speed = 1.0
        self.value = t
        self.events.append("新的一局：极速模式已自动关闭")

    def _run(self):
        last = None
        seen = None             # the timer's last value while on, across hero moves
        still_since = None
        while not self._stop.is_set():
            if self.speed <= 1:
                self.state, self.note, last, seen = OFF, "", None, None
                if self.timer:
                    self.value = read_val(self.h, *self.timer)
                self._sleep(0.5)
                continue
            if self.timer is None or self._recheck:
                found = self._find()
                if self._stop.is_set():
                    break                   # closed while searching: the handle may be gone
                if not found:
                    self._sleep(RETRY)
                    continue
                last, still_since = None, None
            if self._from_offset:
                addr = self.where()             # follow the hero when it moves
                if addr is not None and addr != self.timer[0]:
                    self.timer, last = (addr, "f32"), None
            t = read_val(self.h, *self.timer)
            if t is None or not 0 <= t < MAX_TIME:
                self.events.append("极速模式：计时读不到了，重新找")
                self.timer = None
                continue
            if seen is not None and t < seen - NEW_RUN_DROP:
                self._new_run(t)            # the hero (or the timer) we followed started over
                continue
            if last is None:
                last = t
            moved = abs(t - last) > 1e-3
            new, last = scaled(t, last, self.speed)
            if new is not None:
                write_val(self.h, *self.timer, new)
            self.value = seen = last
            now = time.monotonic()
            if moved:
                still_since = None
                self.state, self.note = ON, ""
            else:
                still_since = still_since or now
                if now - still_since > 1.0:
                    self.state, self.note = PAUSED, "计时暂停中"
                if now - still_since > STALE:
                    # standing still a long time: paused, or a run that isn't ours?
                    still_since = now
                    hud = hud_time(self.h)
                    if hud and abs(hud[0] - t) > 5:
                        if self._from_offset:
                            self._use_offset = False    # not the timer on this version
                            self.events.append(f"极速模式：玩家对象 +0x{RUN_TIME:X} 不是局内计时"
                                               "（游戏版本变了？），改为在内存里找")
                        else:
                            self.events.append("极速模式：局内时间和计时对不上（新的一局？），重新找")
                        self.timer = None
                        continue
            self._sleep(TICK)

    def status(self):
        """(speed, state, timer value or None, note) for the panels."""
        return self.speed, self.state, self.value, self.note
