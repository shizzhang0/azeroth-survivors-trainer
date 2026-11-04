"""Group the exe's `movss [rsi+disp32]` stores by function (from .pdata).

  python group_stores.py [path\\to\\AzerothSurvivors.exe]

The function that writes the stat block back is the one with the most distinct
store offsets; a run of 4-byte-stepped offsets in it is the stat block.
"""
import bisect
import collections
import struct

from scan_stores import default_exe, movss_stores


def pe_sections(d):
    nt = struct.unpack_from("<I", d, 0x3C)[0]
    opt = nt + 24
    magic = struct.unpack_from("<H", d, opt)[0]
    assert magic == 0x20b, f"not a PE32+ image (magic {magic:#x})"
    nsec = struct.unpack_from("<H", d, nt + 6)[0]
    optsz = struct.unpack_from("<H", d, nt + 20)[0]
    exc_rva, exc_size = struct.unpack_from("<II", d, opt + 136)   # exception directory
    secs = []
    for k in range(nsec):
        o = opt + optsz + k * 40
        name = d[o:o + 8].rstrip(b"\x00").decode(errors="replace")
        vsz, va, rsz, rp = struct.unpack_from("<IIII", d, o + 8)
        secs.append((name, va, vsz, rp, rsz))
    return secs, exc_rva, exc_size


def rva2off(secs, rva):
    for _n, va, vsz, rp, rsz in secs:
        if va <= rva < va + max(vsz, rsz):
            return rp + (rva - va)
    return None


def off2rva(secs, off):
    for _n, va, _vsz, rp, rsz in secs:
        if rp <= off < rp + rsz:
            return va + (off - rp)
    return None


def functions(d, secs, exc_rva, exc_size):
    """Sorted (begin, end) RVAs from .pdata RUNTIME_FUNCTION entries."""
    eo = rva2off(secs, exc_rva)
    funcs = []
    for k in range(0, exc_size, 12):
        b, e, _u = struct.unpack_from("<III", d, eo + k)
        if b < e:
            funcs.append((b, e))
    funcs.sort()
    return funcs


def main():
    d = open(default_exe(), "rb").read()
    secs, exc_rva, exc_size = pe_sections(d)
    print("sections:", [(n, hex(v), hex(r)) for n, v, _, r, _ in secs
                        if n in (".text", ".pdata", ".rdata")])
    funcs = functions(d, secs, exc_rva, exc_size)
    starts = [b for b, _e in funcs]
    print(f"functions: {len(funcs)}")

    def func_of(rva):
        i = bisect.bisect_right(starts, rva) - 1
        if i >= 0 and funcs[i][0] <= rva < funcs[i][1]:
            return funcs[i]
        return None

    byf = collections.defaultdict(list)
    n_sites = 0
    for off, disp in movss_stores(d):
        rva = off2rva(secs, off)
        f = func_of(rva) if rva else None
        if f:
            byf[f].append((disp, rva))
            n_sites += 1

    rank = sorted(byf.items(), key=lambda kv: -len({x[0] for x in kv[1]}))
    print(f"\n{n_sites} store(s) inside functions; top functions by distinct offsets:")
    for (b, e), lst in rank[:5]:
        disps = sorted({x[0] for x in lst})
        print(f"\n  func RVA {b:X}..{e:X}  ({e - b} bytes)  stores={len(lst)}  distinct={len(disps)}")
        print("   disps: " + " ".join(f"+0x{x:X}" for x in disps))


if __name__ == "__main__":
    main()
