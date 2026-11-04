"""Azeroth Survivors run-stat trainer (memory, single-player).

  python trainer.py --pid 1234 --show
  python trainer.py --pid 1234 --set dmg=4.0 --set crit=1.0 --set hp=5000
  python trainer.py --pid 1234 --pin dmg=4.0 --pin spd=2.0      # keep re-applying

Offsets were confirmed by writing distinct values into the live struct and
reading them back on the character pane (Character -> Run Stats).
"""
import argparse
import re
import sys
import time

import numpy as np

import memscan

# field -> (byte offset, kind)
# Layout confirmed on v0.2.0-1033-gf79c5e2b (2026-11-02 build). The older
# v0.2.0-977 build had no field at +36, so reg/area/luck/gold sat 4 bytes earlier.
FIELDS = {
    "hp":    (0, "f32"),
    "dmg":   (4, "f32"),
    "cd":    (8, "f32"),     # cooldown multiplier: 0.7 = 30% faster
    "crit":  (12, "f32"),    # 1.0 = 100%
    "cm":    (16, "f32"),    # crit damage multiplier: 2.0 = 200%
    "spd":   (20, "f32"),    # movement multiplier
    "pick":  (24, "f32"),    # pickup radius in yards
    "xp":    (28, "f32"),    # experience multiplier
    "arm":   (32, "f32"),    # damage reduction, capped by the game at 0.6
    "x36":   (36, "f32"),    # v1033 added this; NOT lifesteal (tested: setting it to
                             # 42 while holding it left the panel's lch at 0.6%). Panel
                             # does not display it — treat as unknown, do not write.
    "reg":   (40, "f32"),    # health per second
    "area":  (44, "f32"),    # area multiplier
    "luck":  (48, "f32"),    # 0.30 = +30%, drives card rarity
    "gold":  (52, "f32"),    # gold multiplier
    # tail: not re-verified after the v1033 shift; assumed +4 from the old build
    "echo":  (60, "f32"),
    "heal":  (64, "f32"),
    "bp":    (68, "f32"),    # buff power
    "doth":  (76, "f32"),    # damage-over-time speed
}

HP_MIN, HP_MAX = 40.0, 800000.0


def find_copies(h, hp_hint=None):
    """All memory blocks that look like the run-stat struct.

    When the live max-health is known, only blocks whose first field equals it
    are accepted — that is what separates the real struct from look-alikes.
    """
    out = []
    for base, size, prot, typ in memscan.regions(h):
        off = 0
        while off < size:
            n = min(memscan.CHUNK, size - off)
            raw = memscan.read(h, base + off, n)
            if raw and len(raw) >= 4:
                arr = np.frombuffer(raw, dtype=np.float32)
                if hp_hint:
                    idx = np.flatnonzero(np.isclose(arr, np.float32(hp_hint), rtol=0,
                                                    atol=max(0.5, hp_hint * 1e-4)))
                else:
                    idx = np.flatnonzero((arr > HP_MIN) & (arr < HP_MAX))
                for i in idx:
                    addr = base + off + int(i) * 4
                    if any(abs(addr - c) < 256 for c in out):
                        continue
                    s = [float(v) for v in memscan.floats_at(h, addr, 13)]
                    if len(s) < 13:
                        continue
                    hp, dmg, cd, crit, cm, spd, pick, xp, arm, reg, area, _cnt, gold = s
                    if not (0.2 < dmg < 50 and 0.2 < cd <= 1.2 and 0 <= crit < 6
                            and 1.0 <= cm <= 5 and 0.3 < spd < 6 and 1 <= pick < 80
                            and 0.5 <= xp < 6 and 0 <= arm <= 1.5 and 0 <= reg < 300
                            and 0.2 < area < 6 and 0.5 <= gold < 40):
                        continue
                    if hp_hint and abs(hp - hp_hint) > max(2.0, hp_hint * 0.02):
                        continue
                    out.append(addr)
            off += n
    return out


def hud_hpmax(h):
    """Live max health from the HUD json feed, or None."""
    best = None
    for base, size, prot, typ in memscan.regions(h):
        off = 0
        while off < size:
            n = min(memscan.CHUNK, size - off)
            raw = memscan.read(h, base + off, n)
            if raw:
                text = raw.decode("latin-1")
                start = 0
                while True:
                    i = text.find('{"type":"hud"', start)
                    if i < 0:
                        break
                    start = i + 1
                    seg = text[i:i + 600]
                    m = re.search(r'"hpMax":(-?[0-9.]+)', seg)
                    t = re.search(r'"time":(-?[0-9.]+)', seg)
                    if m:
                        cand = (float(t.group(1)) if t else 0.0, float(m.group(1)))
                        if best is None or cand[0] > best[0] or (
                                cand[0] == best[0] and cand[1] > best[1]):
                            best = cand
            off += n
    if not best:
        return None, None
    return best[1], best[0]


def show(h, copies):
    if not copies:
        print("no stats struct found (is a run in progress?)")
        return
    for c in copies:
        vals = {k: float(memscan.floats_at(h, c + off, 1)[0])
                for k, (off, _k) in FIELDS.items()}
        print(f"0x{c:X}  " + "  ".join(f"{k}={v:.3f}" for k, v in vals.items()
                                        if k in ("hp", "dmg", "cd", "crit", "spd",
                                                 "pick", "xp", "gold", "arm")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE")
    ap.add_argument("--pin", action="append", default=[], metavar="FIELD=VALUE")
    ap.add_argument("--interval", type=float, default=0.25)
    args = ap.parse_args()

    if not args.pid:
        found = memscan.find_pids("AzerothSurvivors")
        if not found:
            sys.exit("game not running (pass --pid)")
        args.pid = found[0][0]

    h = memscan.open_process(args.pid, write=True)
    hp_hint, t = hud_hpmax(h)
    print(f"pid {args.pid}  hud time={t} hpMax={hp_hint}")

    copies = find_copies(h, hp_hint)
    print(f"stats struct copies: {[hex(c) for c in copies]}")
    if not copies:
        sys.exit("could not locate the run stats")

    if args.show or (not args.set and not args.pin):
        show(h, copies)

    def apply(pairs):
        for spec in pairs:
            k, v = spec.split("=")
            if k not in FIELDS:
                sys.exit(f"unknown field {k!r}; known: {', '.join(FIELDS)}")
            off, kind = FIELDS[k]
            val = np.float32(float(v)).tobytes()
            for c in copies:
                memscan.write(h, c + off, val)
            print(f"  {k} -> {v}")

    if args.set:
        print("applying:")
        apply(args.set)
        time.sleep(0.5)
        show(h, copies)

    if args.pin:
        print("pinning (ctrl+c to stop):")
        apply(args.pin)
        try:
            while True:
                time.sleep(args.interval)
                for c in copies:
                    for spec in args.pin:
                        k, v = spec.split("=")
                        off, _ = FIELDS[k]
                        memscan.write(h, c + off, np.float32(float(v)).tobytes())
        except KeyboardInterrupt:
            print("\nstopped")

    memscan.k32.CloseHandle(h)


if __name__ == "__main__":
    main()
