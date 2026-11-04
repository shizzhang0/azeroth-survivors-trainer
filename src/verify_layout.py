"""READ-ONLY layout check after a game update.

Finds the run-stat struct from the live stats line, then reports, field by field,
whether the offsets we rely on still hold. Writes nothing.

  python verify_layout.py [pid]

Known layouts live in offsets.STAT_LAYOUTS; the checker tries each (newest
first) and reports which one fits.
"""
import re
import sys

import game
import memscan
from offsets import GAME_VERSION, STAT_LAYOUTS as LAYOUTS

LINE_RE = re.compile(r"hp=[0-9.]+;dmg=[^;]*;(?:.*?)pet=[0-9.]+")


def stat_lines(h):
    """Every distinct AZS stats line in memory."""
    seen = {}
    for addr, raw in memscan.iter_chunks(h, overlap=4):
        start = 0
        while True:
            i = raw.find(b"luck=", start)
            if i < 0:
                break
            start = i + 1
            chunk = memscan.read(h, max(0, addr + i - 480), 900)
            if not chunk:
                continue
            m = LINE_RE.search(chunk.decode("latin-1"))
            if not m:
                continue
            text = m.group(0)
            if text not in seen:
                seen[text] = {k: float(v) for k, v in re.findall(r"(\w+)=([-\d.]+)", text)}
    return seen


def struct_checks(v, lay):
    """Float-index checks: cm/arm/area/luck/gold must sit where `lay` says.

    hp is deliberately NOT part of the test: the emitted line lags the struct by
    a level-up, so hp can differ by 10% mid-run and would reject a valid struct.
    """
    cm, arm, area, gold = v.get("cm"), v.get("arm"), v.get("area"), v.get("gold")
    if None in (cm, arm, area, gold):
        return None
    luck = v.get("luck", 0.0)
    tol_cm = max(0.01, cm * 0.005)          # panel prints 2 decimals, memory does not
    return [
        (lay["cm"] // 4, cm - tol_cm, cm + tol_cm),
        (lay["arm"] // 4, arm - 0.01, arm + 0.01),
        (lay["area"] // 4, area - 0.02, area + 0.02),
        (lay["luck"] // 4, luck - 0.02, luck + 0.02),
        (lay["gold"] // 4, gold - 0.02, gold + 0.02),
    ]


def find_struct(h, v, lay):
    """Start addresses of blocks matching the stats line under layout `lay`."""
    checks = struct_checks(v, lay)
    if not checks:
        return []
    out = []
    for addr, raw in memscan.iter_chunks(h, overlap=96):
        for i in memscan.match_fields(memscan.as_f32(raw), checks):
            a = addr + int(i) * 4
            if not any(abs(a - c) < 256 for c in out):
                out.append(a)
    return out


def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else game.find_pid()
    if not pid:
        sys.exit("game window not found")
    h = memscan.open_process(pid)
    print(f"pid={pid}   (offsets.py targets {GAME_VERSION})")

    lines = stat_lines(h)
    print(f"{len(lines)} distinct stats line(s)")
    if not lines:
        sys.exit("no stats line — is a run in progress?")

    for text, v in lines.items():
        hit = None
        for name, lay in LAYOUTS.items():
            cands = find_struct(h, v, lay)
            if cands:
                hit = (name, lay, cands)
                break
        if not hit:
            continue
        name, lay, cands = hit
        print(f"\nline: hp={v.get('hp')} luck={v.get('luck')} area={v.get('area')} "
              f"gold={v.get('gold')} cm={v.get('cm')}")
        print(f"layout that fits: {name}")
        addr = cands[0]
        print(f"struct: {[hex(c) for c in cands]}")
        ctx = memscan.floats_at(h, addr, 24)
        print(f"\n{'field':6} {'offset':>6} {'panel':>10} {'memory':>10}")
        # these drift between the moment the game emitted its line and now
        VOLATILE = {"hp", "dmg", "cd", "reg"}
        bad, stale = 0, 0
        for fname, off in lay.items():
            if fname not in v or off // 4 >= len(ctx):
                continue
            got = float(ctx[off // 4])
            exp = v[fname]
            tol = max(0.02 * abs(exp), 0.02)
            ok = abs(got - exp) <= tol
            note = "OK" if ok else ("stale (line lags)" if fname in VOLATILE else "MISMATCH")
            if not ok:
                if fname in VOLATILE:
                    stale += 1
                else:
                    bad += 1
            print(f"{fname:6} {'+%d' % off:>6} {exp:10.4f} {got:10.4f}   {note}")
        print("\ntail: " + "  ".join(
            f"+{o}={float(ctx[o // 4]):.4f}" for o in (52, 56, 60, 64, 68, 72, 76)
            if o // 4 < len(ctx)))
        if bad == 0:
            print(f"VERDICT: offsets still valid"
                  + (f" ({stale} volatile field(s) just lagged the line)" if stale else ""))
        else:
            print(f"VERDICT: {bad} structural field(s) moved — do NOT write until re-identified")
        break
    else:
        print("no struct matched any line (run may be paused / not in a run)")
    memscan.k32.CloseHandle(h)


if __name__ == "__main__":
    main()
