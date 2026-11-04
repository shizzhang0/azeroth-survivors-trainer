"""Azeroth Survivors — player-object tool (gold / health / level / xp / kills / chest price).

This is the second layer: the run-state object that owns the stat block the
trainer writes, found by a structural fingerprint instead of by value guessing.

  python player.py --show
  python player.py --gold 999999
  python player.py --heal
  python player.py --hp 5000 --maxhp 5000
  python player.py --level 30 --xp 0
  python player.py --chest 1          # next chest costs 1 gold (grows again on opening)

Offsets live in offsets.py: confirmed by differential snapshots and a write
test (gold wrote 999999 and the HUD followed; current HP write healed the hero),
and re-checked against the live HUD after each update. Fields listed in
offsets.PLAYER_UNVERIFIED are shown as "?" and refused for writing.
"""
import argparse
import struct
import sys
import time

import numpy as np

import game
import memscan
from offsets import (GAME_VERSION, PLAYER, PLAYER_FINGERPRINT, PLAYER_UNVERIFIED,
                     STAT_RANGES, player_stat)

# Writable run-state fields, in write order (maxhp before anything that reads it).
WRITABLE = {
    "gold": PLAYER["gold"],
    "maxhp": (player_stat("hp"), "<f"),
    "hp": PLAYER["hp"],
    "level": PLAYER["level"],
    "xp": PLAYER["xp"],
    "kills": PLAYER["kills"],
    "rerolls": PLAYER["rerolls"],
    "banishes": PLAYER["banishes"],
    "chest": PLAYER["chest"],
}
MAXHP = player_stat("hp")
LEVEL_K = PLAYER["level"][0] // 4
# one read covers every field we validate or show
OBJ_SIZE = max([off for off, _ in PLAYER.values()] + [player_stat("gold")]) + 4

# structural fingerprint: every check must hold at once (vectorized over memory)
FINGERPRINT = [(player_stat(n) // 4, *STAT_RANGES[n]) for n in PLAYER_FINGERPRINT]
FINGERPRINT.append((MAXHP // 4, 50.0, 1e6))
FP_OVERLAP = (max(k for k, _lo, _hi in FINGERPRINT) + 1) * 4


def get(blob, off, fmt):
    return struct.unpack_from(fmt, blob, off)[0]


def matches_hud(blob, info):
    """Is this candidate object the live hero, judged against the HUD?"""
    gold = get(blob, *PLAYER["gold"])
    kills = get(blob, *PLAYER["kills"])
    lvl = get(blob, *PLAYER["level"])
    cur = get(blob, *PLAYER["hp"])
    mx = get(blob, MAXHP, "<f")
    # kills is a big, slow-moving number: the strongest identity check we have.
    # Fall back to gold when the HUD frame is stale (run just ended / restarted).
    kills_ok = ("kills" in info and
                abs(kills - info["kills"]) <= max(20.0, info["kills"] * 0.05))
    gold_ok = ("gold" in info and
               abs(gold - info["gold"]) <= max(2000.0, info["gold"] * 0.1))
    if not (kills_ok or gold_ok):
        return False
    return 1 <= lvl < 200 and 50 < mx < 1e6 and 0 <= cur <= mx * 1.2


def hud_score(blob, info):
    """How many HUD numbers this candidate agrees with, 0-4.

    matches_hud() is only a gate: its gold fallback is loose on purpose, so a
    stray block with a gold-like number can pass it (v1118: level 1, max hp 98,
    kills garbage, picked over the real hero because it sat at a lower
    address). The HUD frame is read before a ~30 s sweep, so the run moves on
    meanwhile — slack is generous, but a wrong block misses by a mile.
    """
    def near(field_value, key, slack):
        return key in info and abs(field_value - info[key]) <= slack(info[key])

    return sum((
        near(get(blob, *PLAYER["level"]), "level", lambda x: 3),
        near(get(blob, *PLAYER["kills"]), "kills", lambda x: max(100.0, x * 0.5)),
        near(get(blob, *PLAYER["gold"]), "gold", lambda x: max(200.0, x * 0.5)),
        near(get(blob, MAXHP, "<f"), "hpMax", lambda x: max(50.0, x * 0.25)),
    ))


def locate(h):
    """Player object by structural fingerprint, verified against the HUD."""
    # a newer partial frame can carry gold but not kills: insist on a whole one
    info = game.read_hud(h, require=("gold", "kills", "level"))
    if not info:
        sys.exit("no HUD frame — is a run in progress?")

    hits, checked = {}, set()            # address -> HUD score
    for addr, raw in memscan.iter_chunks(h, overlap=FP_OVERLAP):
        idx = memscan.match_fields(memscan.as_f32(raw), FINGERPRINT)
        if not len(idx):
            continue
        lv = np.frombuffer(raw, dtype=np.int32, count=len(raw) // 4)[idx + LEVEL_K]
        for i in idx[(lv >= 1) & (lv < 200)]:
            a = addr + int(i) * 4
            if a in checked:            # seen again in a chunk overlap
                continue
            checked.add(a)
            blob = memscan.read(h, a, OBJ_SIZE)
            if not blob or len(blob) < OBJ_SIZE or not matches_hud(blob, info):
                continue
            if not any(abs(a - b) < 512 for b in hits):
                hits[a] = hud_score(blob, info)
    if not hits:
        sys.exit("player object not found (run may be paused or just started)")
    # best agreement with the HUD wins; ties go to the lower address as before
    best = max(sorted(hits), key=lambda a: hits[a])
    return best, info


def show(h, base):
    blob = memscan.read(h, base, OBJ_SIZE)
    if not blob or len(blob) < OBJ_SIZE:
        print("  (player object unreadable)")
        return
    p = {k: ("?" if k in PLAYER_UNVERIFIED else get(blob, off, fmt))
         for k, (off, fmt) in PLAYER.items()}
    s = {k: get(blob, player_stat(k), "<f")
         for k in ("hp", "dmg", "crit", "cm", "spd", "luck", "area")}
    print(f"  gold   = {p['gold']}")
    print(f"  health = {p['hp']:.1f} / {s['hp']:.1f}")
    print(f"  level  = {p['level']}   xp = {p['xp']}")
    print(f"  kills  = {p['kills']}   rerolls = {p['rerolls']}   banishes = {p['banishes']}")
    print(f"  chest  = {p['chest']} gold")
    print(f"  dmg={s['dmg']:.3f} crit={s['crit']:.3f} cm={s['cm']:.2f} spd={s['spd']:.3f} "
          f"luck={s['luck']:.3f} area={s['area']:.3f}")


def fmt_hud(info):
    def g(k, spec):
        return format(info[k], spec) if k in info else "?"
    return (f"gold={g('gold', '.0f')} level={g('level', '.0f')} "
            f"hp={g('hp', '.1f')}/{g('hpMax', '.0f')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pid", type=int)
    ap.add_argument("--show", action="store_true")
    for name, (_off, fmt) in WRITABLE.items():
        ap.add_argument(f"--{name}", type=float if fmt == "<f" else int)
    ap.add_argument("--heal", action="store_true", help="set health to max health")
    args = ap.parse_args()

    writes = [(n, getattr(args, n)) for n in WRITABLE if getattr(args, n) is not None]
    blocked = [n for n, _v in writes if n in PLAYER_UNVERIFIED]
    if blocked:
        sys.exit(f"{', '.join(blocked)}: offset not verified on {GAME_VERSION}, refusing to write")
    pid = args.pid or game.find_pid()
    if not pid:
        sys.exit("game window not found")
    h = memscan.open_process(pid, write=bool(writes or args.heal))
    try:
        print(f"pid={pid}", flush=True)
        print("locating player object (one full memory sweep)...", flush=True)
        base, info = locate(h)
        print(f"player object @ 0x{base:X}   (hud: {fmt_hud(info)})")
        show(h, base)

        for name, value in writes:
            off, fmt = WRITABLE[name]
            memscan.write(h, base + off, struct.pack(fmt, value))
            print(f"{name} -> {value}", flush=True)
        if args.heal:
            mx = struct.unpack("<f", memscan.read(h, base + MAXHP, 4))[0]
            memscan.write(h, base + PLAYER["hp"][0], struct.pack("<f", mx))
            print(f"healed to {mx:.1f}", flush=True)

        if writes or args.heal:
            time.sleep(0.4)
            print("after:")
            show(h, base)
    finally:
        memscan.k32.CloseHandle(h)


if __name__ == "__main__":
    main()
