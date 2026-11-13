"""READ-ONLY layout check after a game update.

Finds the run-stat struct from the live stats line, then reports, field by field,
whether the offsets we rely on still hold. Writes nothing.

  python verify_layout.py [pid]

The stat-block layout checked is offsets.STAT, the current game's.
"""
import re
import sys

import game
import memscan
import player
from offsets import PLAYER, PLAYER_UNVERIFIED, STAT

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


# these drift between the moment the game emitted its line and now
VOLATILE = {"hp", "dmg", "cd", "reg"}


def compare_line(v, lay, ctx):
    """Field-by-field panel vs memory: (rows, structural mismatches, stale volatiles)."""
    rows, bad, stale = [], 0, 0
    for fname, off in lay.items():
        if fname not in v or off // 4 >= len(ctx):
            continue
        got = float(ctx[off // 4])
        exp = v[fname]
        ok = abs(got - exp) <= max(0.02 * abs(exp), 0.02)
        if ok:
            note = "OK"
        elif fname in VOLATILE:
            note, stale = "stale (line lags)", stale + 1
        else:
            note, bad = "MISMATCH", bad + 1
        rows.append((fname, off, exp, got, note))
    return rows, bad, stale


# player-object field -> (HUD key, allowed |memory - hud|). The HUD frame can date
# from early in a ~30 s memory sweep, so counters that grow get generous slack; a moved
# offset reads garbage (or 0) and still fails by a mile.
PLAYER_VS_HUD = {
    "level": ("level", lambda x: 2),
    "gold": ("gold", lambda x: max(200.0, x * 0.5)),
    "kills": ("kills", lambda x: max(100.0, x * 0.5)),
    "rerolls": ("rerolls", lambda x: 2),
    "chest": ("chestCost", lambda x: max(20.0, x * 0.5)),   # grows each chest opened
}


def check_player(blob, hud):
    """[(field, memory, hud, verdict)] for run-state fields the HUD also prints."""
    rows = []
    for field, (key, slack) in PLAYER_VS_HUD.items():
        mem = player.get(blob, *PLAYER[field])
        if field in PLAYER_UNVERIFIED:
            rows.append((field, mem, hud.get(key), "unverified (not written)"))
        elif key not in hud:
            rows.append((field, mem, None, "no HUD value"))
        else:
            ok = abs(mem - hud[key]) <= slack(abs(hud[key]))
            rows.append((field, mem, hud[key], "OK" if ok else "MISMATCH"))
    cur = player.get(blob, *PLAYER["hp"])
    mx = player.get(blob, player.MAXHP, "<f")
    rows.append(("hp", cur, mx, "OK" if 0 <= cur <= mx * 1.2 else "MISMATCH"))
    return rows


def verify_player(h):
    """Second half of the check: run-state offsets against the live HUD."""
    print("\nplayer object (one more memory sweep)...", flush=True)
    base, hud = player.locate(h)
    blob = memscan.read(h, base, player.OBJ_SIZE)
    print(f"player object @ 0x{base:X}")
    print(f"\n{'field':8} {'offset':>7} {'hud':>10} {'memory':>12}")
    bad = 0
    for field, mem, ref, verdict in check_player(blob, hud):
        off = PLAYER[field][0]
        ref_s = "-" if ref is None else f"{ref:.0f}" if field != "hp" else f"max {ref:.0f}"
        print(f"{field:8} {'+0x%X' % off:>7} {ref_s:>10} {mem:12.0f}   {verdict}")
        bad += verdict == "MISMATCH"
    for field in sorted(PLAYER_UNVERIFIED - set(PLAYER_VS_HUD)):
        print(f"{field:8} {'+0x%X' % PLAYER[field][0]:>7} {'-':>10} {'?':>12}   unverified (not written)")
    if bad:
        print(f"VERDICT: {bad} player field(s) moved — do NOT write them until re-identified")
    else:
        print("VERDICT: player offsets match the HUD")
    return bad


def main():
    game.utf8_console()
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else game.find_pid()
    if not pid:
        sys.exit("game window not found")
    h = memscan.open_process(pid)
    print(f"pid={pid}")

    lines = stat_lines(h)
    print(f"{len(lines)} distinct stats line(s)")
    if not lines:
        sys.exit("no stats line — is a run in progress?")

    lay = STAT
    for v in lines.values():
        cands = find_struct(h, v, lay)
        if not cands:
            continue
        print("the stat block matches offsets.STAT")
        addr = cands[0]
        print(f"struct: {[hex(c) for c in cands]}")
        ctx = memscan.floats_at(h, addr, 24)
        # Memory keeps several stats lines from different moments; older ones
        # predate the last card pick. Judge the struct by the line it fits best.
        v = min(lines.values(), key=lambda line: compare_line(line, lay, ctx)[1:])
        rows, bad, stale = compare_line(v, lay, ctx)
        print(f"\nclosest of {len(lines)} line(s): hp={v.get('hp')} luck={v.get('luck')} "
              f"area={v.get('area')} gold={v.get('gold')} cm={v.get('cm')}")
        print(f"\n{'field':6} {'offset':>6} {'panel':>10} {'memory':>10}")
        for fname, off, exp, got, note in rows:
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
        print("no struct matched offsets.STAT (stat block moved after an update? "
              "or the run is paused / not started)")
    verify_player(h)
    memscan.k32.CloseHandle(h)


if __name__ == "__main__":
    main()
