"""Find the run timer (HUD "time", what boss arrivals are scheduled on) and run it faster.

Only the timer is touched: combat, cooldowns and movement keep their speed. A boss due
at 8:00 on the timer arrives after 4 real minutes at 2x.

The timer's type isn't known, so three are tried: f32, f64, and a Duration (u64 seconds +
u32 nanoseconds, what a Bevy Stopwatch holds). A candidate must read about the HUD time
and grow with wall time; the one that keeps a +N s write (and drags any copies along)
is the master.

  python find_runtimer.py                       find candidates (read-only)
  python find_runtimer.py --test                also add 10 s to each, see which one sticks
  python find_runtimer.py --speed 2 [--for 300] find, then run the timer at 2x until Ctrl+C
  python find_runtimer.py --add 60              find, then add 60 s once

Be in active play when it starts: not on a card pick, a menu or the pause screen.
"""
import argparse
import struct
import sys
import time

import numpy as np

import game
import memscan

SLACK = 1.5          # s: the HUD frame lags the timer by up to an update
LIVE_WAIT = 1.0
TICK = 0.05          # s between speed-loop steps
KINDS = ("f32", "f64", "dur")
NANOS = 1_000_000_000


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


def decode(kind, raw):
    if raw is None:
        return None
    if kind == "f32" and len(raw) >= 4:
        return float(struct.unpack_from("<f", raw)[0])
    if kind == "f64" and len(raw) >= 8:
        return struct.unpack_from("<d", raw)[0]
    if kind == "dur" and len(raw) >= 12:
        secs, nanos = struct.unpack_from("<QI", raw)
        return secs + nanos / NANOS if nanos < NANOS else None
    return None


def encode(kind, value):
    if kind == "f32":
        return struct.pack("<f", value)
    if kind == "f64":
        return struct.pack("<d", value)
    secs = int(value)
    return struct.pack("<QI", secs, min(NANOS - 1, int(round((value - secs) * NANOS))))


SIZE = {"f32": 4, "f64": 8, "dur": 12}


def scaled(t, last, speed, max_step=0.5):
    """One speed-loop step. t: the timer now; last: what we left it at.
    Returns (value to write or None, new last). The game's own growth since our last
    write is scaled by speed; no growth (card pick, pause) adds nothing, and a jump
    (new run, someone else's write) is taken as is."""
    grown = t - last
    if 0 < grown <= max_step:
        new = t + (speed - 1) * grown
        return new, new
    return None, t


# ---- process side -------------------------------------------------------------

def read_val(h, addr, kind):
    return decode(kind, memscan.read(h, addr, SIZE[kind]))


def write_val(h, addr, kind, value):
    return memscan.write(h, addr, encode(kind, value))


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
        g = v1 - v0
        return (g >= 0.3 * dt) & (g <= dt + 0.3)


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


def find(h):
    print("读 HUD 局内时间（约 15 秒）……", flush=True)
    hud = hud_time(h)
    if not hud:
        sys.exit("没读到 HUD 局内时间：是不是不在一局里？")
    t0, w0 = hud
    print(f"HUD 局内时间 {t0:.1f} 秒。扫描内存找计时变量（约 15 秒）……", flush=True)
    scan = RangeScan(lambda now: (t0 - SLACK, t0 + (now - w0) + SLACK))
    memscan.sweep(h, [scan.visitor()])
    n = sum(len(offs) for _a, hits, _s in scan.chunks for offs, _v in hits.values())
    print(f"数值对得上的槽 {n} 个，再扫一遍看哪些在随时间走……", flush=True)
    hits = sorted(set(regrown(h, scan.chunks)))
    print(f"还在走的 {len(hits)} 个，逐个隔 {LIVE_WAIT:.0f} 秒复查……", flush=True)

    # one more look a second apart, read by read: drops whatever grew by chance
    r0 = [(read_val(h, a, k), time.perf_counter()) for a, k in hits]
    time.sleep(LIVE_WAIT)
    live = []
    for (a, k), (v0, s0) in zip(hits, r0):
        v1, s1 = read_val(h, a, k), time.perf_counter()
        if v0 is not None and v1 is not None and still_growing(v0, v1, s1 - s0):
            live.append((a, k))
    return live


def probe(h, live, add):
    """Add `add` s to each candidate alone, see which stick and which others follow,
    then take it back off. The master sticks and drags the most copies along."""
    master = []
    for a, k in live:
        before = {c: read_val(h, *c) for c in live}
        if before[(a, k)] is None:
            continue
        write_val(h, a, k, before[(a, k)] + add)
        time.sleep(0.3)
        after = {c: read_val(h, *c) for c in live}
        jumped = [c for c in live
                  if None not in (before[c], after[c]) and after[c] - before[c] >= add - 0.5]
        stuck = (a, k) in jumped
        if stuck:
            write_val(h, a, k, read_val(h, a, k) - add)        # undo
            master.append(((a, k), len(jumped)))
        if stuck or len(jumped) > 1:
            print(f"  0x{a:X} {k}: {'保持住了' if stuck else '被改回去了'}，"
                  f"一起跳的 {len(jumped)} 个", flush=True)
        time.sleep(0.3)
    print(f"  试了 {len(live)} 个，保持住的 {len(master)} 个（都已减回去）")
    return [c for c, _n in sorted(master, key=lambda m: -m[1])]


def run_fast(h, addr, kind, speed, secs):
    end = None if secs is None else time.perf_counter() + secs
    until = "" if secs is None else f"{secs:.0f} 秒后停，"
    print(f"\n计时 {speed}x 中，{until}按 Ctrl+C 停（已经加上去的时间保留）。", flush=True)
    last = read_val(h, addr, kind)
    t_start, w_start, report = last, time.perf_counter(), time.perf_counter() + 10
    try:
        while memscan.alive(h) and (end is None or time.perf_counter() < end):
            time.sleep(TICK)
            t = read_val(h, addr, kind)
            if t is None:
                print("计时变量读不到了（这局结束了？），停。")
                return
            new, last = scaled(t, last, speed)
            if new is not None:
                write_val(h, addr, kind, new)
            now = time.perf_counter()
            if now >= report:
                print(f"  局内 {int(last // 60)}:{last % 60:04.1f}，"
                      f"平均 {(last - t_start) / (now - w_start):.2f}x", flush=True)
                report = now + 10
    except KeyboardInterrupt:
        pass
    print("停止加速。")


def main():
    game.utf8_console()
    p = argparse.ArgumentParser(description="找局内计时并让它走快（战斗速度不变）")
    p.add_argument("--test", action="store_true", help="逐个 +10 秒，看哪个是真正的计时")
    p.add_argument("--speed", type=float, metavar="倍率", help="让计时按这个倍率走")
    p.add_argument("--for", dest="secs", type=float, metavar="秒", help="--speed 持续多久（默认一直）")
    p.add_argument("--add", type=float, metavar="秒", help="一次性给计时加这么多秒")
    p.add_argument("--at", type=lambda s: int(s, 0), metavar="地址",
                   help="已知计时变量的地址（上一次找到的），跳过查找")
    p.add_argument("--kind", choices=KINDS, default="f32", help="--at 的类型（默认 f32）")
    p.add_argument("--pid", type=int)
    args = p.parse_args()
    if args.speed is not None and not 1 <= args.speed <= 20:
        sys.exit("--speed 只接受 1 ~ 20")

    pid = args.pid or game.find_pid()
    if not pid:
        sys.exit("没找到游戏进程")
    write = args.test or args.speed is not None or args.add is not None
    h = memscan.open_process(pid, write=write)
    try:
        if args.at is not None:
            v = read_val(h, args.at, args.kind)
            if v is None or not 0 <= v < 86400:
                sys.exit(f"0x{args.at:X} 读到的不像计时：{v}")
            print(f"计时变量 0x{args.at:X}（{args.kind}）= {v:.1f} 秒")
            if args.add:
                write_val(h, args.at, args.kind, v + args.add)
                print(f"加了 {args.add:.0f} 秒。")
            if args.speed is not None:
                run_fast(h, args.at, args.kind, args.speed, args.secs)
            return
        live = find(h)
        print(f"\n在走的候选 {len(live)} 个：")
        for a, k in live:
            print(f"  0x{a:X} {k} = {read_val(h, a, k):.3f}")
        if not write:
            return
        if not live:
            sys.exit("没有候选，不写。确认在正常战斗中再跑一次。")
        if len(live) > 400:
            sys.exit("候选太多，不写。")

        print("\n逐个 +10 秒试：", flush=True)
        master = probe(h, live, 10.0)
        if not master:
            sys.exit("没有哪个能保持住，计时可能不是这几种类型。")
        addr, kind = master[0]
        print(f"计时变量：0x{addr:X}（{kind}）")
        if args.test:
            # the HUD should jump with it: add 30 s and compare
            write_val(h, addr, kind, read_val(h, addr, kind) + 30)
            mine = read_val(h, addr, kind)
            hud = hud_time(h)
            mine_now = read_val(h, addr, kind)
            if hud:
                print(f"给它加了 30 秒。HUD 局内时间 {hud[0]:.1f} 秒，计时变量 {mine:.1f}～{mine_now:.1f} 秒"
                      "（HUD 落在这个区间里，说明 HUD 跟着它走；这 30 秒不减回去）")
        if args.add:
            write_val(h, addr, kind, read_val(h, addr, kind) + args.add)
            print(f"加了 {args.add:.0f} 秒。")
        if args.speed is not None:
            run_fast(h, addr, kind, args.speed, args.secs)
    finally:
        memscan.k32.CloseHandle(h)


if __name__ == "__main__":
    main()
