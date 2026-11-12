"""Find Bevy's virtual clock (Time<Virtual>) and test whether scaling it speeds the game up.

Bevy 0.18 (the exe carries bevy_time-0.18.1) advances its virtual clock every frame by
    delta = min(real frame time, max_delta) * relative_speed      (0 while paused)
and Update / FixedUpdate read that clock. relative_speed is an f64 in the Time<Virtual>
resource, so writing 2.0 there should run the game at 2x: a plain memory write, no
injection. Resources stay where they are for the whole session (components move, these
don't), so one scan per game launch would do.

Fingerprint; rustc picks the field order, so no fixed offsets are assumed:
  wrap_period        Duration 3600 s     every Time<T>
  max_delta          Duration 0.25 s     Time<Virtual>, Bevy's default
  relative_speed,    two f64 1.0, 8 or   Time<Virtual> only; the plain Time (a copy made
  effective_speed    24 bytes apart      every frame) has the same wrap_period but no pair
  elapsed_secs_f64   an f64 that grows with wall time (tells the live clock from leftovers)

  python find_clock.py              scan and list candidates (read-only)
  python find_clock.py --test       also measure 1x and 2x (~30 s each), restore 1x
  python find_clock.py --hold 2 --for 60
                                    after 15 s (time to switch to the game), 2x for 60 s,
                                    then restore 1x; without --for until Ctrl+C

Be in active play when it runs: not on a card pick, a menu or the pause screen.
"""
import argparse
import struct
import sys
import time

import numpy as np

import game
import memscan

ONE = 0x3FF0_0000_0000_0000      # f64 1.0
WRAP_SECS = 3600
MAX_DELTA_NANOS = 250_000_000
LOW32 = np.uint64(0xFFFF_FFFF)
NEAR = 256                       # bytes either side of a pair that belong to its Time
PAIR_GAPS = (8, 24)              # the two speeds: adjacent, or a Duration between them
MAXD_NEAR = 48                   # Virtual is 40 bytes: max_delta sits right by the pair
LIVE_WAIT = 0.5


# ---- pure parts (tested offline) -------------------------------------------------

def u64s(raw):
    return np.frombuffer(raw, dtype="<u8", count=len(raw) // 8)


def duration_hits(q, secs, nanos):
    """u64 indices where q holds Duration{secs, nanos}, in either field order.
    A Duration is a u64 and a u32 plus 4 bytes of padding; padding isn't checked."""
    if len(q) < 2:
        return np.empty(0, dtype=np.intp)
    a, b = q[:-1], q[1:]
    secs_first = (a == np.uint64(secs)) & ((b & LOW32) == np.uint64(nanos))
    nanos_first = ((a & LOW32) == np.uint64(nanos)) & (b == np.uint64(secs))
    # Duration{3600, 0} secs-first at i+1 also reads as nanos-first at i when the
    # word before it happens to end in zeros: keep the secs-first reading
    nanos_first[:-1] &= ~secs_first[1:]
    return np.flatnonzero(secs_first | nanos_first)


def clusters(addrs, gap=NEAR):
    """Sorted addresses grouped into [lo, hi] spans, merging hits closer than gap."""
    out = []
    for a in sorted(set(addrs)):
        if out and a - out[-1][1] <= gap:
            out[-1][1] = a
        else:
            out.append([a, a])
    return out


def analyze(base, raw):
    """Speed-pair candidates in one window of memory (base must be 8-aligned).

    Returns [{"pair": (a, b), "wrap": addr, "max_delta": addr or None}]: two f64 1.0
    PAIR_GAPS apart, with a 3600 s Duration within NEAR bytes.
    """
    q = u64s(raw)
    at = lambda idx: [base + 8 * int(i) for i in idx]      # noqa: E731
    ones = at(np.flatnonzero(q == np.uint64(ONE)))
    wraps = at(duration_hits(q, WRAP_SECS, 0))
    maxds = at(duration_hits(q, 0, MAX_DELTA_NANOS))
    out = []
    for a in ones:
        for b in ones:
            if b - a not in PAIR_GAPS:
                continue
            wrap = min(wraps, key=lambda w: abs(w - a), default=None)
            if wrap is None or abs(wrap - a) > NEAR:
                continue
            maxd = [m for m in maxds if abs(m - a) <= MAXD_NEAR or abs(m - b) <= MAXD_NEAR]
            out.append({"pair": (a, b), "wrap": wrap, "max_delta": maxd[0] if maxd else None})
    return out


def growing(base, raw0, raw1, dt):
    """f64 slots that grew by about dt seconds between two reads: elapsed clocks.
    A clock only moves once a frame, so each read can lag by a frame."""
    n = min(len(raw0), len(raw1)) // 8
    a = np.frombuffer(raw0, dtype="<f8", count=n)
    b = np.frombuffer(raw1, dtype="<f8", count=n)
    with np.errstate(invalid="ignore", over="ignore"):
        d = b - a
        ok = (np.isfinite(a) & (a > 0.05) & (a < 1e7)
              & (np.abs(d - dt) <= 0.15 + 0.1 * dt))
    return [base + 8 * int(i) for i in np.flatnonzero(ok)]


# ---- process side -------------------------------------------------------------

def read_f64(h, addr):
    raw = memscan.read(h, addr, 8)
    return struct.unpack("<d", raw)[0] if raw and len(raw) == 8 else None


_restore = {}       # addr -> value to put back on exit


def write_f64(h, addr, value):
    _restore.setdefault(addr, 1.0)
    return memscan.write(h, addr, struct.pack("<d", value))


def restore_all(h):
    for addr, value in _restore.items():
        memscan.write(h, addr, struct.pack("<d", value))
    _restore.clear()


class AnchorScan:
    """memscan.sweep visitor: every 3600 s and 0.25 s Duration in memory."""

    def __init__(self):
        self.hits = []

    def visitor(self):
        return 16, self

    def __call__(self, addr, raw, end):
        q = u64s(raw[:end])
        for secs, nanos in ((WRAP_SECS, 0), (0, MAX_DELTA_NANOS)):
            self.hits.extend(addr + 8 * int(i) for i in duration_hits(q, secs, nanos))


def read_window(h, lo, hi):
    """Bytes of [lo, hi], shrinking toward the middle if an edge isn't readable."""
    lo -= lo % 8
    for pad in (0, NEAR // 2, NEAR - 8):
        raw = memscan.read(h, lo + pad, hi - lo - 2 * pad)
        if raw:
            return lo + pad, raw
    return None, None


def find_candidates(h, anchors):
    seen, out = set(), []
    for lo, hi in clusters(anchors):
        base, raw = read_window(h, lo - NEAR, hi + NEAR + 16)
        if raw is None:
            continue
        for c in analyze(base, raw):
            if c["pair"] not in seen:
                seen.add(c["pair"])
                out.append(c)
    return out


class StampedHud:
    """memscan.sweep visitor: every HUD frame's run time, with the wall time it was
    read. HUD frames live in buffers the game keeps replacing, so a frame can't be
    watched at a fixed address: each measurement sweeps again."""

    def __init__(self):
        self.frames = []        # (run time, perf_counter when read)

    def visitor(self):
        return game.HUD_SPAN, self

    def __call__(self, addr, raw, end):
        now = time.perf_counter()
        i = raw.find(game.HUD_MARK, 0, end)
        while i >= 0:
            got = game.parse_hud(raw[i:min(i + game.HUD_SPAN, end)])
            if got and "time" in got[0]:
                self.frames.append((got[0]["time"], now))
            i = raw.find(game.HUD_MARK, i + 1, end)


def pick_stamp(frames, prev, now, max_speed=10.0):
    """The current run's newest (run time, read at) among frames, given an earlier
    stamp of this run: run time can't go back or run faster than max_speed, which
    keeps leftover frames of earlier runs out."""
    t_prev, w_prev = prev
    hi = t_prev + max_speed * (now - w_prev) + 5.0
    ok = [f for f in frames if t_prev - 1.0 <= f[0] <= hi]
    return max(ok) if ok else None


def hud_stamp(h, prev):
    s = StampedHud()
    memscan.sweep(h, [s.visitor()])
    return pick_stamp(s.frames, prev, time.perf_counter())


def live_check(h, cands):
    """Mark each candidate's elapsed slots: f64s that grow with wall time."""
    def snap():
        return [read_window(h, c["pair"][0] - NEAR, c["pair"][0] + NEAR) for c in cands], \
            time.perf_counter()

    w0, t0 = snap()
    time.sleep(LIVE_WAIT)
    w1, t1 = snap()
    for c, (b0, r0), (b1, r1) in zip(cands, w0, w1):
        c["elapsed"] = growing(b0, r0, r1, t1 - t0) if r0 and r1 and b0 == b1 else []


def measure(h, elapsed, hud_prev):
    """(virtual clock rate, HUD run-time rate, latest HUD stamp) against wall time,
    over two HUD sweeps (~15 s each). A rate is None when it can't be read."""
    e0, w0 = read_f64(h, elapsed), time.perf_counter()
    s0 = hud_stamp(h, hud_prev) if hud_prev else None
    s1 = hud_stamp(h, s0) if s0 else None
    e1, w1 = read_f64(h, elapsed), time.perf_counter()
    v = None if e0 is None or e1 is None else (e1 - e0) / (w1 - w0)
    u = (s1[0] - s0[0]) / (s1[1] - s0[1]) if s0 and s1 and s1[1] > s0[1] else None
    return v, u, s1 or s0 or hud_prev


def find_relative(h, pair):
    """Which slot of the pair is relative_speed: the one whose 2.0 sticks and is
    copied into the other (effective_speed) on the next frame. None if neither."""
    for slot, other in (pair, pair[::-1]):
        write_f64(h, slot, 2.0)
        time.sleep(0.3)
        a, b = read_f64(h, slot), read_f64(h, other)
        if a == 2.0 and b == 2.0:
            return slot, other
        write_f64(h, slot, 1.0)
        time.sleep(0.2)
    return None


def fmt(rate):
    return "读不到" if rate is None else f"{rate:.2f}x"


def show(c):
    a, b = c["pair"]
    md = f"0x{c['max_delta']:X}" if c["max_delta"] else "无"
    el = ", ".join(f"0x{e:X}" for e in c["elapsed"]) or "无"
    print(f"  速度对 0x{a:X} / 0x{b:X}  wrap_period 0x{c['wrap']:X}  "
          f"max_delta {md}  在走的时钟 {el}")


def main():
    game.utf8_console()
    p = argparse.ArgumentParser(description="找 Bevy 的虚拟时钟并测试变速")
    p.add_argument("--test", action="store_true", help="1 倍速、2 倍速各测约 30 秒，然后还原")
    p.add_argument("--hold", type=float, metavar="倍率", help="不测量，直接切到这个倍率，Ctrl+C 还原")
    p.add_argument("--for", dest="secs", type=float, metavar="秒",
                   help="--hold 保持多少秒后自动还原（默认一直保持）")
    p.add_argument("--wait", type=float, default=15, metavar="秒",
                   help="--hold 改倍率前先等几秒，留时间切回游戏（默认 15）")
    p.add_argument("--pid", type=int)
    args = p.parse_args()
    if args.hold is not None and not 0.1 <= args.hold <= 10:
        sys.exit("--hold 只接受 0.1 ~ 10")

    pid = args.pid or game.find_pid()
    if not pid:
        sys.exit("没找到游戏进程")
    write = args.test or args.hold is not None
    h = memscan.open_process(pid, write=write)
    print(f"pid={pid}，扫描内存中（约 15～90 秒）……", flush=True)

    t = time.perf_counter()
    anchors, hud = AnchorScan(), game.HudScan()
    memscan.sweep(h, [anchors.visitor(), hud.visitor()])
    print(f"扫描完成 {time.perf_counter() - t:.0f} 秒：Duration 锚点 {len(anchors.hits)} 个，"
          f"HUD 帧 {len(hud.spots)} 个", flush=True)

    cands = find_candidates(h, anchors.hits)
    live_check(h, cands)
    live = [c for c in cands if c["elapsed"]]
    print(f"\n候选 {len(cands)} 个，其中时钟在走的 {len(live)} 个：")
    for c in sorted(cands, key=lambda c: (not c["elapsed"], c["max_delta"] is None)):
        show(c)
    frame = hud.pick(h, require=("time",))
    hud_prev = (frame["time"], time.perf_counter()) if frame else None
    print(f"HUD 局内时间：{frame['time']:.1f} 秒" if frame else "HUD 局内时间：没找到")

    if not write:
        if len(live) == 1:
            print("\n只有一个活的候选，可以加 --test 试变速。")
        return
    if len(live) != 1:
        sys.exit("\n活的候选不是正好一个，不往游戏里写。确认在正常战斗中（不在选卡/菜单/暂停）再跑一次。")

    c = live[0]
    elapsed = c["elapsed"][0]
    try:
        if args.hold is not None:
            found = find_relative(h, c["pair"])
            if not found:
                sys.exit("两个槽都不像 relative_speed（写 2.0 后没被复制到另一个），已还原。")
            print(f"relative_speed = 0x{found[0]:X}")
            print(f"{args.wait:.0f} 秒后切到 {args.hold}x，现在切回游戏。", flush=True)
            time.sleep(args.wait)
            hold(h, found[0], elapsed, hud_prev, args.hold, args.secs)
            return

        print("\n测 1 倍速基线（扫两次内存，约 30 秒）……", flush=True)
        v, u, hud_prev = measure(h, elapsed, hud_prev)
        print(f"1 倍速：虚拟时钟 {fmt(v)}，HUD 局内时间 {fmt(u)}")

        found = find_relative(h, c["pair"])
        if not found:
            sys.exit("两个槽都不像 relative_speed（写 2.0 后没被复制到另一个），已还原。")
        rel, eff = found
        print(f"relative_speed = 0x{rel:X}，effective_speed = 0x{eff:X}")

        print("测 2 倍速（约 30 秒）……", flush=True)
        v, u, hud_prev = measure(h, elapsed, hud_prev)
        print(f"2 倍速：虚拟时钟 {fmt(v)}，HUD 局内时间 {fmt(u)}")
        write_f64(h, rel, 1.0)
        time.sleep(0.3)
        print(f"已还原 1 倍速，effective_speed 读回 {read_f64(h, eff)}")
        print("\n判读：虚拟时钟 ≈2x 说明找对了时钟；HUD 局内时间也 ≈2x 说明游戏逻辑跟着它走。"
              "\n      再用 --hold 2 亲眼看看怪物、技能冷却、动画是不是都变快了。")
    finally:
        restore_all(h)
        memscan.k32.CloseHandle(h)


def hold(h, rel, elapsed, hud_prev, speed, secs=None):
    until = "" if secs is None else f"{secs:.0f} 秒后自动还原，"
    print(f"\n现在 {speed}x，{until}按 Ctrl+C 随时还原 1 倍速并退出。约每 30 秒报一次实测倍率。",
          flush=True)
    write_f64(h, rel, speed)
    end = None if secs is None else time.perf_counter() + secs
    try:
        while memscan.alive(h) and (end is None or time.perf_counter() < end):
            if end is not None and end - time.perf_counter() < 35:
                time.sleep(max(0.0, end - time.perf_counter()))     # too short to measure
                break
            v, u, hud_prev = measure(h, elapsed, hud_prev)
            cur = read_f64(h, rel)
            note = ""
            if cur != speed:
                note = f"  ← 游戏把倍率改成了 {cur}，重新写入"
                write_f64(h, rel, speed)
            print(f"  虚拟时钟 {fmt(v)}，HUD 局内时间 {fmt(u)}{note}", flush=True)
    except KeyboardInterrupt:
        pass
    print("还原 1 倍速。")


if __name__ == "__main__":
    main()
