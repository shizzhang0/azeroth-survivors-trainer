"""Azeroth Survivors run-stat trainer (memory, single-player).

  python trainer.py --show
  python trainer.py --set dmg=4.0 --set crit=1.0 --set hp=5000
  python trainer.py --pin dmg=4.0 --pin spd=2.0      # keep re-applying

Before anything is written the trainer sweeps game memory twice (HUD, then the
stat block), 30-90 s, printing progress. Pinning starts at the
"pinning every ... s" line and lasts while the window stays open.

Offsets live in offsets.py and were confirmed by writing distinct values into
the live struct and reading them back on the character pane (Character -> Run
Stats). While pinning, every target is re-validated before each write and
dropped if it stops looking like a stat block (run ended, memory reused).
"""
import argparse
import sys
import time

import numpy as np

import game
import memscan
from offsets import HP_RANGE, STAT, STAT_INT, STAT_RANGES, STAT_READONLY, STAT_TAIL

FIELDS = {**STAT, **STAT_TAIL}
SPAN = max(FIELDS.values()) // 4 + 1        # floats per stat block (incl. tail)
SHOWN = ("hp", "dmg", "cd", "crit", "spd", "pick", "xp", "gold", "arm", "luck")
MAX_MISSES = 3          # consecutive failed re-validations before a target is dropped
# The game recomputes the block every 1-3 s. Measured: at 0.25 s the
# panel flickered back to the real value for up to 168 ms; at 0.02 s, <= 22 ms.
PIN_INTERVAL = 0.02


def stat_checks(hp_hint=None, skip=()):
    """(float index, lo, hi) checks a live stat block satisfies.

    With the HUD's max health known, the hp slot must equal it — that is what
    separates the real struct from look-alikes. Fields in `skip` (the ones we
    are pinning, possibly to out-of-range values) are not checked.
    """
    checks = []
    if "hp" not in skip:
        if hp_hint:
            tol = max(0.5, hp_hint * 1e-4)
            checks.append((0, hp_hint - tol, hp_hint + tol))
        else:
            checks.append((0, *HP_RANGE))
    for name, (lo, hi) in STAT_RANGES.items():
        if name in STAT and name not in skip:
            checks.append((STAT[name] // 4, lo, hi))
    return checks


def block_ok(arr, checks):
    """Does the block starting at arr[0] pass every check?"""
    idx = memscan.match_fields(arr, checks)
    return len(idx) > 0 and idx[0] == 0


def find_copies(h, hp_hint=None):
    """All memory blocks that look like the run-stat struct."""
    checks = stat_checks(hp_hint)
    out = []
    for addr, raw in memscan.iter_chunks(h, overlap=SPAN * 4):
        for i in memscan.match_fields(memscan.as_f32(raw), checks):
            a = addr + int(i) * 4
            if not any(abs(a - c) < 256 for c in out):
                out.append(a)
    return out


def parse_specs(specs):
    """['dmg=4', ...] -> [(name, offset, f32 bytes)]; exits on a bad spec."""
    out = []
    for spec in specs:
        k, sep, v = spec.partition("=")
        k = k.strip()
        if not sep:
            sys.exit(f"bad spec {spec!r}: want FIELD=VALUE")
        if k not in FIELDS:
            sys.exit(f"unknown field {k!r}; known: {', '.join(f for f in FIELDS if f not in STAT_READONLY)}")
        if k in STAT_READONLY:
            sys.exit(f"{k} is read-only: its meaning is unknown, writing it is not supported")
        try:
            val = float(v)
        except ValueError:
            sys.exit(f"bad value in {spec!r}: not a number")
        if k in STAT_INT:               # e.g. targets: a whole number, stored as i32
            if val != int(val):
                sys.exit(f"bad value in {spec!r}: {k} needs a whole number")
            out.append((k, FIELDS[k], np.int32(int(val)).tobytes()))
        else:
            out.append((k, FIELDS[k], np.float32(val).tobytes()))
    return out


def write_all(h, copies, pins):
    for c in copies:
        for _k, off, data in pins:
            memscan.write(h, c + off, data)


def show(h, copies):
    if not copies:
        print("no stats struct found (is a run in progress?)")
        return
    for c in copies:
        arr = memscan.floats_at(h, c, SPAN)
        if len(arr) < SPAN:
            print(f"0x{c:X}  (unreadable)")
            continue
        print(f"0x{c:X}  " + "  ".join(f"{k}={float(arr[FIELDS[k] // 4]):.3f}" for k in SHOWN))


def pin(h, copies, pins, interval, skip):
    checks = stat_checks(None, skip)
    misses = dict.fromkeys(copies, 0)
    print(f"pinning every {interval:g} s (ctrl+c to stop):")
    for k, _off, data in pins:
        print(f"  {k} -> {float(np.frombuffer(data, np.int32 if k in STAT_INT else np.float32)[0]):g}")
    sys.stdout.flush()
    try:
        while True:
            for c in list(misses):
                if block_ok(memscan.floats_at(h, c, SPAN), checks):
                    misses[c] = 0
                else:
                    misses[c] += 1
                    if misses[c] >= MAX_MISSES:
                        del misses[c]
                        print(f"  0x{c:X} no longer looks like a stat block — dropped", flush=True)
            if not misses:
                print("no valid target left (run ended?). Not writing; "
                      "start the trainer again inside the new run.")
                return
            write_all(h, [c for c, n in misses.items() if n == 0], pins)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped")


def main():
    game.utf8_console()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pid", type=int)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE")
    ap.add_argument("--pin", action="append", default=[], metavar="FIELD=VALUE")
    ap.add_argument("--interval", type=float, default=PIN_INTERVAL,
                    help=f"seconds between pin writes (default {PIN_INTERVAL})")
    args = ap.parse_args()

    sets, pins = parse_specs(args.set), parse_specs(args.pin)
    pid = args.pid or game.find_pid()
    if not pid:
        sys.exit("game not running (pass --pid)")

    h = memscan.open_process(pid, write=bool(sets or pins))
    try:
        print(f"pid {pid}: reading the HUD (memory sweep 1/2)...", flush=True)
        hud = game.read_hud(h, require=("hpMax",))
        hp_hint = hud["hpMax"] if hud else None
        print(f"hud time={hud.get('time') if hud else None} hpMax={hp_hint}", flush=True)
        if (sets or pins) and not hp_hint:
            sys.exit("no HUD frame — refusing to write blind. Is a run in progress?")

        print("locating the stat block (memory sweep 2/2)...", flush=True)
        copies = find_copies(h, hp_hint)
        print(f"stats struct copies: {[hex(c) for c in copies]}")
        if not copies:
            sys.exit("could not locate the run stats")

        if args.show or not (sets or pins):
            show(h, copies)
        if sets:
            print("applying:")
            write_all(h, copies, sets)
            for k, _off, _data in sets:
                print(f"  {k} written")
            time.sleep(0.5)
            show(h, copies)
        if pins:
            # fields we wrote may sit outside the normal ranges: don't judge by them
            pin(h, copies, pins, args.interval, skip={k for k, _o, _d in sets + pins})
    finally:
        memscan.k32.CloseHandle(h)


if __name__ == "__main__":
    main()
