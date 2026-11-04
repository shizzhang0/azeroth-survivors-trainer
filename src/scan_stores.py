"""Count `movss [rsi+disp32], xmm` stores in the game exe, by displacement.

  python scan_stores.py [path\\to\\AzerothSurvivors.exe]

Pattern: F3 0F 11 <modrm> <disp32>
  F3 0F 11   movss m32, xmm
  modrm      mod=10, rm=110 -> [rsi + disp32] (reg = xmm0..7)
  disp32     0x100..0x10000, 4-byte aligned
The exe path defaults to DEFAULT_EXE (override with AZS_EXE or argv[1]).
Pure stdlib: no numpy needed.
"""
import collections
import os
import re
import struct
import sys

MOVSS_RSI = re.compile(rb"(?=\xF3\x0F\x11[\x86\x8E\x96\x9E\xA6\xAE\xB6\xBE](.{4}))", re.S)
DISP_MIN, DISP_MAX = 0x100, 0x10000
DEFAULT_EXE = os.environ.get(
    "AZS_EXE", r"C:\Program Files (x86)\AzerothSurvivors\AzerothSurvivors.exe")


def default_exe():
    return sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXE


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
