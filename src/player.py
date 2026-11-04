"""Azeroth Survivors — player-object tool (gold / health / level / xp / kills).

This is the second layer: the run-state object that owns the stat block the
trainer writes, found by a structural fingerprint instead of by value guessing.

  python player.py --show
  python player.py --gold 999999
  python player.py --heal
  python player.py --hp 5000 --maxhp 5000
  python player.py --level 30 --xp 0

Offsets confirmed on v0.2.0-1036 by differential snapshots and a write test
(gold wrote 999999 and the HUD followed; current HP write healed the hero).
"""
import argparse
import ctypes
import re
import struct
import sys
import time
from ctypes import wintypes

import numpy as np

import memscan

# run-state fields (relative to the player-object base)
OFF = {
    "level": 0x58C,      # i32
    "xp": 0x590,         # i32  (current experience, resets on level up)
    "hp": 0x594,         # f32  current health
    "gold": 0x5A4,       # i32  coin amount
    "rerolls": 0x658,    # i32  rerolls left (watched 5->4 on a real reroll)
    "banishes": 0xF5C,   # i32  banishes left (watched 3->2 on a real banish)
    "kills": 0x974,      # i32
}
# the stat block that owns the 11 derived attributes
STAT = {
    "max_hp": 0x5F4, "dmg": 0x5F8, "cd": 0x5FC, "crit": 0x600, "cm": 0x604,
    "spd": 0x608, "pick": 0x60C, "xp_mult": 0x610, "arm": 0x614,
    "x36": 0x618, "reg": 0x61C, "area": 0x620, "gold_mult": 0x628,
}
# structural ranges: a real player object satisfies all of these at once.
# Deliberately wide — values grow a lot over a run (level 40+ characters carry
# pickup ranges and regen in the hundreds).
RANGES = {
    0x604: (1.0, 20.0),    # crit damage multiplier
    0x608: (0.5, 20.0),    # movement
    0x60C: (1.0, 800.0),   # pickup radius
    0x610: (0.5, 20.0),    # xp multiplier
    0x614: (0.0, 2.0),     # armor
    0x61C: (0.0, 5000.0),  # regen
    0x620: (0.2, 30.0),    # area
}


def find_pid():
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    found = []

    def cb(hwnd, _l):
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(n + 1, 2))
        u32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value == "Azeroth Survivors":
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append(pid.value)
        return True

    u32.EnumWindows(CB(cb), 0)
    return found[0] if found else None


def hud(h):
    """Newest HUD frame: live numbers the game itself prints."""
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
                    seg = text[i:i + 700]
                    d = {}
                    for k in ("time", "level", "xp", "xpNext", "hp", "hpMax",
                              "gold", "chestCost", "kills", "enemies", "rerolls"):
                        m = re.search(rf'"{k}":(-?[0-9.]+)', seg)
                        if m:
                            d[k] = float(m.group(1))
                    if "gold" in d and (best is None or d.get("time", 0) > best.get("time", 0)):
                        best = d
            off += n
    return best


def locate(h):
    """Player object by structural fingerprint, verified against the HUD."""
    info = hud(h)
    if not info:
        sys.exit("no HUD frame — is a run in progress?")

    max_f = max(RANGES) // 4 + 4
    cands = []
    for base, size, prot, typ in memscan.regions(h):
        off = 0
        while off < size:
            n = min(memscan.CHUNK, size - off)
            raw = memscan.read(h, base + off, n)
            if raw and len(raw) >= max_f * 4:
                arr = np.frombuffer(raw, dtype=np.float32)
                score = np.zeros(len(arr), dtype=np.int16)
                for byte_off, (lo, hi) in RANGES.items():
                    fi = byte_off // 4
                    m = ((arr > lo) & (arr < hi)).astype(np.int16)
                    score[:len(arr) - fi] += m[fi:]
                for i in np.flatnonzero(score >= len(RANGES)):
                    cands.append(base + off + int(i) * 4)
            off += n

    hits = []
    seen = []
    for a in cands:
        if any(abs(a - s) < 512 for s in seen):
            continue
        seen.append(a)
        try:
            gold = struct.unpack("<I", memscan.read(h, a + OFF["gold"], 4))[0]
            kills = struct.unpack("<I", memscan.read(h, a + OFF["kills"], 4))[0]
            lvl = struct.unpack("<I", memscan.read(h, a + OFF["level"], 4))[0]
            mx = struct.unpack("<f", memscan.read(h, a + STAT["max_hp"], 4))[0]
            cur = struct.unpack("<f", memscan.read(h, a + OFF["hp"], 4))[0]
        except Exception:
            continue
        # kills is a big, slow-moving number: the strongest identity check we have.
        # Fall back to gold when the HUD frame is stale (run just ended / restarted).
        kills_ok = ("kills" in info and
                    abs(kills - info["kills"]) <= max(20.0, info["kills"] * 0.05))
        gold_ok = ("gold" in info and abs(gold - info["gold"]) <= max(2000.0,
                                                                     info["gold"] * 0.1))
        if not (kills_ok or gold_ok):
            continue
        if not (1 <= lvl < 200 and 50 < mx < 1e6 and 0 <= cur <= mx * 1.2):
            continue
        hits.append(a)
    if not hits:
        sys.exit("player object not found (run may be paused or just started)")
    return hits[0], info


def rd_i(h, a, off):
    return struct.unpack("<I", memscan.read(h, a + off, 4))[0]


def rd_f(h, a, off):
    return struct.unpack("<f", memscan.read(h, a + off, 4))[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--gold", type=int)
    ap.add_argument("--hp", type=float)
    ap.add_argument("--maxhp", type=float)
    ap.add_argument("--heal", action="store_true")
    ap.add_argument("--level", type=int)
    ap.add_argument("--xp", type=int)
    ap.add_argument("--kills", type=int)
    ap.add_argument("--rerolls", type=int)
    ap.add_argument("--banishes", type=int)
    args = ap.parse_args()

    pid = args.pid or find_pid()
    if not pid:
        sys.exit("game window not found")
    write = any(v is not None for v in (args.gold, args.hp, args.maxhp, args.level,
                                        args.xp, args.kills, args.rerolls,
                                        args.banishes)) or args.heal
    h = memscan.open_process(pid, write=write)
    print(f"pid={pid}", flush=True)
    print("locating player object (one full memory sweep)...", flush=True)
    base, info = locate(h)
    print(f"player object @ 0x{base:X}   (hud: gold={info.get('gold'):.0f} "
          f"level={info.get('level'):.0f} hp={info.get('hp'):.1f}/{info.get('hpMax'):.0f})")

    def show():
        cur = rd_f(h, base, OFF["hp"])
        mx = rd_f(h, base, STAT["max_hp"])
        print(f"  gold   = {rd_i(h, base, OFF['gold'])}")
        print(f"  health = {cur:.1f} / {mx:.1f}")
        print(f"  level  = {rd_i(h, base, OFF['level'])}   xp = {rd_i(h, base, OFF['xp'])}")
        print(f"  kills  = {rd_i(h, base, OFF['kills'])}   rerolls = {rd_i(h, base, OFF['rerolls'])}"
              f"   banishes = {rd_i(h, base, OFF['banishes'])}")
        print(f"  dmg={rd_f(h, base, STAT['dmg']):.3f} crit={rd_f(h, base, STAT['crit']):.3f} "
              f"cm={rd_f(h, base, STAT['cm']):.2f} spd={rd_f(h, base, STAT['spd']):.3f} "
              f"luck={rd_f(h, base, 0x624):.3f} area={rd_f(h, base, STAT['area']):.3f}")

    show()
    changed = False
    if args.gold is not None:
        memscan.write(h, base + OFF["gold"], struct.pack("<I", args.gold))
        print(f"gold -> {args.gold}", flush=True)
        changed = True
    if args.maxhp is not None:
        memscan.write(h, base + STAT["max_hp"], struct.pack("<f", args.maxhp))
        print(f"max hp -> {args.maxhp}", flush=True)
        changed = True
    if args.hp is not None:
        memscan.write(h, base + OFF["hp"], struct.pack("<f", args.hp))
        print(f"health -> {args.hp}", flush=True)
        changed = True
    if args.heal:
        mx = rd_f(h, base, STAT["max_hp"])
        memscan.write(h, base + OFF["hp"], struct.pack("<f", mx))
        print(f"healed to {mx:.1f}", flush=True)
        changed = True
    if args.level is not None:
        memscan.write(h, base + OFF["level"], struct.pack("<I", args.level))
        print(f"level -> {args.level}", flush=True)
        changed = True
    if args.xp is not None:
        memscan.write(h, base + OFF["xp"], struct.pack("<I", args.xp))
        print(f"xp -> {args.xp}", flush=True)
        changed = True
    if args.kills is not None:
        memscan.write(h, base + OFF["kills"], struct.pack("<I", args.kills))
        print(f"kills -> {args.kills}", flush=True)
        changed = True
    if args.rerolls is not None:
        memscan.write(h, base + OFF["rerolls"], struct.pack("<I", args.rerolls))
        print(f"rerolls -> {args.rerolls}", flush=True)
        changed = True
    if args.banishes is not None:
        memscan.write(h, base + OFF["banishes"], struct.pack("<I", args.banishes))
        print(f"banishes -> {args.banishes}", flush=True)
        changed = True

    if changed:
        time.sleep(0.4)
        print("after:")
        show()
    memscan.k32.CloseHandle(h)


if __name__ == "__main__":
    main()
