import struct, collections
p = r"C:\Program Files (x86)\AzerothSurvivors\AzerothSurvivors.exe"
d = open(p, "rb").read()

nt = struct.unpack_from("<I", d, 0x3C)[0]
opt = nt + 24
magic = struct.unpack_from("<H", d, opt)[0]
assert magic == 0x20b, hex(magic)
nsec = struct.unpack_from("<H", d, nt+6)[0]
optsz = struct.unpack_from("<H", d, nt+20)[0]
exc_rva, exc_size = struct.unpack_from("<II", d, opt+136)
secs = []
for k in range(nsec):
    o = opt + optsz + k*40
    name = d[o:o+8].rstrip(b"\x00").decode()
    vsz, va, rsz, rp = struct.unpack_from("<IIII", d, o+8)
    secs.append((name, va, vsz, rp, rsz))
print("sections:", [(n, hex(v), hex(r)) for n,v,_,r,_ in secs if n in ('.text','.pdata','.rdata')])

def rva2off(rva):
    for n,va,vsz,rp,rsz in secs:
        if va <= rva < va + max(vsz, rsz):
            return rp + (rva - va)
    return None

def off2rva(off):
    for n,va,vsz,rp,rsz in secs:
        if rp <= off < rp + rsz:
            return va + (off - rp)
    return None

# functions from .pdata
funcs = []
eo = rva2off(exc_rva)
for k in range(0, exc_size, 12):
    b, e, u = struct.unpack_from("<III", d, eo+k)
    if b < e: funcs.append((b, e))
print(f"functions: {len(funcs)}")

def func_of(rva):
    for b, e in funcs:
        if b <= rva < e: return (b, e)
    return None

MODRM = {0x86,0x8E,0x96,0x9E,0xA6,0xAE,0xB6,0xBE}
sites = []
i = d.find(b"\xf3\x0f\x11")
while i >= 0:
    if i + 8 <= len(d) and d[i+3] in MODRM:
        disp = struct.unpack_from("<i", d, i+4)[0]
        if 0x100 <= disp <= 0x10000 and disp % 4 == 0:
            rva = off2rva(i)
            if rva:
                f = func_of(rva)
                if f: sites.append((f, disp, rva))
    i = d.find(b"\xf3\x0f\x11", i+1)

byf = collections.defaultdict(list)
for f, disp, rva in sites:
    byf[f].append((disp, rva))
rank = sorted(byf.items(), key=lambda kv: -len(set(x[0] for x in kv[1])))
print(f"\n{len(sites)} store(s) inside functions; top functions by distinct offsets:")
for (b, e), lst in rank[:5]:
    disps = sorted(set(x[0] for x in lst))
    print(f"\n  func RVA {b:X}..{e:X}  ({e-b} bytes)  stores={len(lst)}  distinct={len(disps)}")
    print("   disps: " + " ".join(f"+0x{x:X}" for x in disps))
