"""READ-ONLY layout check after a game update.

Finds the run-stat struct from the live stats line, then reports, field by field,
whether the offsets we rely on still hold. Writes nothing.

  python verify_layout.py [pid]
"""
import re
import sys

import numpy as np

import memscan

# Known layouts. The v1033 build inserted a field at +36 and pushed everything
# after `arm` four bytes along; the checker tries each and reports which fits.
LAYOUTS = {
    "v0.2.0-1036": {
        "hp": 0, "dmg": 4, "cd": 8, "crit": 12, "cm": 16, "spd": 20, "pick": 24,
        "xp": 28, "arm": 32, "x36": 36, "reg": 40, "area": 44, "luck": 48, "gold": 52,
    },
    "v0.2.0-1033": {
        "hp": 0, "dmg": 4, "cd": 8, "crit": 12, "cm": 16, "spd": 20, "pick": 24,
        "xp": 28, "arm": 32, "x36": 36, "reg": 40, "area": 44, "luck": 48, "gold": 52,
    },
    "v0.2.0-977": {
        "hp": 0, "dmg": 4, "cd": 8, "crit": 12, "cm": 16, "spd": 20, "pick": 24,
        "xp": 28, "arm": 32, "reg": 36, "area": 40, "luck": 44, "gold": 48,
    },
}
LAYOUT = LAYOUTS["v0.2.0-1033"]
TOL = {"cnt": 0.0}          # cnt is an integer field: exact


def find_pid():
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u32.EnumWindows.argtypes = [CB, wintypes.LPARAM]
    u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    found = []

    def cb(hwnd, lparam):
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


def stat_lines(h):
    """Every distinct AZS stats line in memory."""
    seen = {}
    for base, size, prot, typ in memscan.regions(h):
        off = 0
        while off < size:
            n = min(memscan.CHUNK, size - off)
            raw = memscan.read(h, base + off, n)
            if raw:
                start = 0
                while True:
                    i = raw.find(b"luck=", start)
                    if i < 0:
                        break
                    start = i + 1
                    chunk = memscan.read(h, max(0, base + off + i - 480), 900)
                    if not chunk:
                        continue
                    m = re.search(r"hp=[0-9.]+;dmg=[^;]*;(?:.*?)pet=[0-9.]+",
                                  chunk.decode("latin-1"))
                    if not m:
                        continue
                    text = m.group(0)
                    if text in seen:
                        continue
                    seen[text] = {k: float(v) for k, v in
                                  re.findall(r"(\w+)=([-\d.]+)", text)}
            off += n
    return seen


def find_struct(h, v, lay=None):
    """Blocks whose cm/arm/area/luck/gold sit at the expected offsets.

    hp is deliberately NOT part of the test: the emitted line lags the struct by
    a level-up, so hp can differ by 10% mid-run and would reject a valid struct.
    """
    lay = lay or LAYOUT
    out = []
    cm, arm, area, gold = v.get("cm"), v.get("arm"), v.get("area"), v.get("gold")
    if None in (cm, arm, area, gold):
        return out
    tol_cm = max(0.01, cm * 0.005)          # panel prints 2 decimals, memory does not
    for base, size, prot, typ in memscan.regions(h):
        off = 0
        while off < size:
            n = min(memscan.CHUNK, size - off)
            raw = memscan.read(h, base + off, n)
            if raw and len(raw) >= 4:
                arr = np.frombuffer(raw, dtype=np.float32)
                for i in np.flatnonzero(np.isclose(arr, np.float32(cm), rtol=0, atol=tol_cm)):
                    addr = base + off + int(i) * 4 - lay["cm"]
                    if addr <= 0 or any(abs(addr - c) < 256 for c in out):
                        continue
                    ctx = memscan.floats_at(h, addr, 20)
                    if len(ctx) < 20:
                        continue
                    if (abs(float(ctx[lay["arm"] // 4]) - arm) <= 0.01
                            and abs(float(ctx[lay["area"] // 4]) - area) <= 0.02
                            and abs(float(ctx[lay["luck"] // 4]) - v.get("luck", 0.0)) <= 0.02
                            and abs(float(ctx[lay["gold"] // 4]) - gold) <= 0.02):
                        out.append(addr)
            off += n
    return out


def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else find_pid()
    if not pid:
        sys.exit("game window not found")
    h = memscan.open_process(pid)
    print(f"pid={pid}")

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
