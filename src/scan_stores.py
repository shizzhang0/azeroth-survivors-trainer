"""Count `movss [rsi+disp32], xmm` stores in the game exe, by displacement.

  python scan_stores.py [path\\to\\AzerothSurvivors.exe]

Pattern: F3 [REX] 0F 11 <modrm> <disp32>
  F3 0F 11   movss m32, xmm
  REX        optional 40/42/44/46: R picks xmm8..15, X is unused here. Never B
             (that turns rsi into r14) or W. v1127 stores xp from xmm13
             (F3 44 0F 11 AE 9C 06 00 00), which the plain form missed.
  modrm      mod=10, rm=110 -> [rsi + disp32]
  disp32     0x100..0x10000, 4-byte aligned
The exe is argv[1], else game_exe(): the AZS_EXE environment variable, else the
exe the GUI was pointed at once. The repo holds no install path.
Pure stdlib: no numpy needed.
"""
import collections
import os
import re
import struct
import sys

MOVSS_RSI = re.compile(
    rb"(?=\xF3[\x40\x42\x44\x46]?\x0F\x11[\x86\x8E\x96\x9E\xA6\xAE\xB6\xBE](.{4}))", re.S)
DISP_MIN, DISP_MAX = 0x100, 0x10000


# The exe the user picked in the GUI, kept per user, outside the repo.
REMEMBERED = os.path.join(os.environ.get("APPDATA", ""), "azeroth-survivors-trainer", "game-exe.txt")


class ExeNotSet(RuntimeError):
    pass


def game_exe(remembered=None):
    """The game exe: AZS_EXE, else the one remember_exe() stored (if it still
    exists). The install path is the user's, not the repo's, so with neither
    this is an error rather than a guess."""
    path = os.environ.get("AZS_EXE")
    if path:
        return path
    try:
        with open(remembered or REMEMBERED, encoding="utf-8") as f:
            path = f.read().strip()
    except OSError:
        path = ""
    if path and os.path.isfile(path):
        return path
    raise ExeNotSet("game exe unknown: pass the game's AzerothSurvivors.exe, set AZS_EXE "
                    "to it, or pick it once in the GUI's save panel")


def remember_exe(path, remembered=None):
    """Store `path` for game_exe() to find next time."""
    remembered = remembered or REMEMBERED
    os.makedirs(os.path.dirname(remembered), exist_ok=True)
    with open(remembered, "w", encoding="utf-8") as f:
        f.write(path + "\n")


def default_exe():
    """For the command-line scans: argv[1], else game_exe()."""
    if len(sys.argv) > 1:
        return sys.argv[1]
    try:
        return game_exe()
    except ExeNotSet as e:
        sys.exit(str(e))


def movss_stores(d):
    """[(file offset, disp)] for every matching store (overlapping hits included)."""
    out = []
    for m in MOVSS_RSI.finditer(d):
        disp = struct.unpack("<i", m.group(1))[0]
        if DISP_MIN <= disp <= DISP_MAX and disp % 4 == 0:
            out.append((m.start(), disp))
    return out


def main():
    path = default_exe()
    d = open(path, "rb").read()
    hits = movss_stores(d)
    total = d.count(b"\xf3\x0f\x11")
    print(f"movss store 总数: {total}")
    print(f"命中 [rsi+disp 0x100..0x10000] 的: {len(hits)}")
    c = collections.Counter(disp for _i, disp in hits)
    print(f"不同偏移数: {len(c)}")
    print("出现最多的偏移:")
    for disp, ctr in c.most_common(12):
        print(f"  +0x{disp:X}  x{ctr}")


if __name__ == "__main__":
    main()
