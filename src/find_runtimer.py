"""极速模式 from the command line: find the run timer and run it faster (see turbo.py).

Only the timer is touched: combat, cooldowns and movement keep their speed. A boss due
at 8:00 on the timer arrives after 2 real minutes at 4x.

  python find_runtimer.py                       find candidates (read-only)
  python find_runtimer.py --test                also probe each (+10 s, taken back off)
                                                and report which one is the timer
  python find_runtimer.py --speed 4 [--for 300] find, then run the timer at 4x until Ctrl+C
  python find_runtimer.py --add 60              find, then add 60 s once
  python find_runtimer.py --at 0x... --speed 4  timer already known: skip the search

Be in active play when it starts: not on a card pick, a menu or the pause screen.
"""
import argparse
import sys
import time

import game
import memscan
import turbo
from turbo import KINDS, read_val, write_val


def run_fast(h, addr, kind, speed, secs):
    end = None if secs is None else time.perf_counter() + secs
    until = "" if secs is None else f"{secs:.0f} 秒后停，"
    print(f"\n计时 {speed}x 中，{until}按 Ctrl+C 停（已经加上去的时间保留）。", flush=True)
    last = read_val(h, addr, kind)
    t_start, w_start, report = last, time.perf_counter(), time.perf_counter() + 10
    try:
        while memscan.alive(h) and (end is None or time.perf_counter() < end):
            time.sleep(turbo.TICK)
            t = read_val(h, addr, kind)
            if t is None:
                print("计时变量读不到了（这局结束了？），停。")
                return
            new, last = turbo.scaled(t, last, speed)
            if new is not None:
                write_val(h, addr, kind, new)
            now = time.perf_counter()
            if now >= report:
                print(f"  局内 {turbo.fmt_time(last)}，"
                      f"平均 {(last - t_start) / (now - w_start):.2f}x", flush=True)
                report = now + 10
    except KeyboardInterrupt:
        pass
    print("停止加速。")


def main():
    game.utf8_console()
    p = argparse.ArgumentParser(description="找局内计时并让它走快（战斗速度不变）")
    p.add_argument("--test", action="store_true", help="逐个 +10 秒再减回去，看哪个是真正的计时")
    p.add_argument("--speed", type=float, metavar="倍率", help="让计时按这个倍率走")
    p.add_argument("--for", dest="secs", type=float, metavar="秒", help="--speed 持续多久（默认一直）")
    p.add_argument("--add", type=float, metavar="秒", help="一次性给计时加这么多秒")
    p.add_argument("--at", type=lambda s: int(s, 0), metavar="地址",
                   help="已知计时变量的地址（上一次找到的），跳过查找")
    p.add_argument("--kind", choices=KINDS, default="f32", help="--at 的类型（默认 f32）")
    p.add_argument("--pid", type=int)
    args = p.parse_args()
    if args.speed is not None and not 1 <= args.speed <= turbo.MAX_SPEED:
        sys.exit(f"--speed 只接受 1 ~ {turbo.MAX_SPEED:g}")

    pid = args.pid or game.find_pid()
    if not pid:
        sys.exit("没找到游戏进程")
    write = args.test or args.speed is not None or args.add is not None
    h = memscan.open_process(pid, write=write)
    say = lambda msg: print(msg + "……", flush=True)      # noqa: E731
    try:
        if args.at is not None:
            timer = (args.at, args.kind)
            v = read_val(h, *timer)
            if v is None or not 0 <= v < turbo.MAX_TIME:
                sys.exit(f"0x{args.at:X} 读到的不像计时：{v}")
        else:
            try:
                live = turbo.find_live(h, say)
            except turbo.TurboError as e:
                sys.exit(str(e))
            print(f"\n在走的候选 {len(live)} 个：")
            for a, k in live:
                print(f"  0x{a:X} {k} = {read_val(h, a, k):.3f}")
            if not write:
                return
            if len(live) > turbo.MAX_CANDIDATES:
                sys.exit("候选太多，不写。")
            add = 10.0 if args.test else turbo.PROBE_ADD
            print(f"\n逐个 +{add:.0f} 秒再减回去：", flush=True)
            results = turbo.probe(h, live, add, early=not args.test)
            for (a, k), stuck, jumped in results:
                if stuck or jumped > 1:
                    print(f"  0x{a:X} {k}: {'保持住了' if stuck else '被改回去了'}，一起跳的 {jumped} 个")
            timer = turbo.pick_master(results)
            if not timer:
                sys.exit("没有哪个能保持住，计时可能不是这几种类型。")
        print(f"计时变量：0x{timer[0]:X}（{timer[1]}）= {read_val(h, *timer):.1f} 秒")
        if args.add:
            write_val(h, *timer, read_val(h, *timer) + args.add)
            print(f"加了 {args.add:.0f} 秒。")
        if args.speed is not None:
            run_fast(h, *timer, args.speed, args.secs)
    finally:
        memscan.k32.CloseHandle(h)


if __name__ == "__main__":
    main()
