import struct, collections
p = r"C:\Program Files (x86)\AzerothSurvivors\AzerothSurvivors.exe"
d = open(p, "rb").read()

MODRM = {0x86,0x8E,0x96,0x9E,0xA6,0xAE,0xB6,0xBE}
hits = []
i = d.find(b"\xf3\x0f\x11")
n = 0
while i >= 0:
    if i + 8 <= len(d) and d[i+3] in MODRM:
        disp = struct.unpack_from("<i", d, i+4)[0]
        if 0x100 <= disp <= 0x10000 and disp % 4 == 0:
            hits.append((i, disp, d[i+3]))
    n += 1
    i = d.find(b"\xf3\x0f\x11", i+1)

print(f"movss store 总数: {n}")
print(f"命中 [rsi+disp 0x100..0x10000] 的: {len(hits)}")
c = collections.Counter(h[1] for h in hits)
print(f"不同偏移数: {len(c)}")
print("出现最多的偏移:")
for disp, ctr in c.most_common(12):
    print(f"  +0x{disp:X}  x{ctr}")
