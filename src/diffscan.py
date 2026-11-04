"""Differential memory scan: find fields that advance like the run timer.

Two passes over the small heap regions, with per-region wall-clock timestamps.
A field whose f32 value grows by about the elapsed wall time between the two
reads is a timer candidate -- and the run's `elapsed` is the anchor we want.

  python diffscan.py --census <pid>     # just size the job
  python diffscan.py <pid> [max_mb]
"""
import struct
import sys
import time

import numpy as np

import game
import memscan

MODE_CENSUS = "--census" in sys.argv
argv = [a for a in sys.argv[1:] if not a.startswith("--")]
pid = int(argv[0])
MAX_MB = float(argv[1]) if len(argv) > 1 else 8.0

h = memscan.open_process(pid)

regions = []
total = 0
for base, size, prot, typ in memscan.regions(h):
    if size > MAX_MB * 1048576:
        continue
    regions.append((base, size))
    total += size

print(f"pid={pid}  regions<= {MAX_MB}MB: {len(regions)}  total {total/1048576:.0f} MB",
      flush=True)
if MODE_CENSUS:
    big = [(b, s) for b, s in regions]
    big.sort(key=lambda x: -x[1])
    for b, s in big[:8]:
        print(f"  0x{b:X}  {s/1048576:.2f} MB")
    sys.exit(0)

CAP = 2 * 1024 ** 3
snap = []
used = 0
t_start = time.time()


hud = game.read_hud(h, require=("time",))
t_hud = hud["time"] if hud else None
print(f"hud time: {t_hud}", flush=True)
for base, size in regions:
    raw = memscan.read(h, base, size)
    if not raw:
        continue
    snap.append((base, raw, time.time()))
    used += len(raw)
    if used > CAP:
        print(f"cap reached at {used/1048576:.0f} MB, stopping pass 1", flush=True)
        break
print(f"pass 1 done: {len(snap)} regions, {used/1048576:.0f} MB, "
      f"{time.time()-t_start:.1f}s", flush=True)

time.sleep(1.0)

found = []
for idx, (base, old, t1) in enumerate(snap):
    new = memscan.read(h, base, len(old))
    t2 = time.time()
    if not new or len(new) < 16:
        continue
    delta = t2 - t1
    n = min(len(old), len(new)) // 4
    a = np.frombuffer(old[:n * 4], dtype=np.float32)
    b = np.frombuffer(new[:n * 4], dtype=np.float32)
    with np.errstate(invalid="ignore"):
        d = b.astype(np.float64) - a.astype(np.float64)
        bad = np.abs(d - delta) > max(0.15, delta * 0.02)
        ok = (~bad) & np.isfinite(a) & (a > 0.5) & (a < 200000) & (d > 0.5)
    for i in np.flatnonzero(ok):
        found.append((base + int(i) * 4, float(a[i]), float(delta), idx))
    if idx % 200 == 0:
        print(f"  compared {idx}/{len(snap)}", flush=True)

print(f"\ntimer candidates: {len(found)}", flush=True)
if t_hud:
    near = [c for c in found if t_hud - 5.0 <= c[1] <= t_hud + 900.0]
    print(f"  of which near the run clock ({t_hud:.0f}s): {len(near)}", flush=True)
    found = near
found.sort(key=lambda x: abs(x[1] - (t_hud or 0.0)))
for addr, val, delta, idx in found[:8]:
    ctx = memscan.floats_at(h, addr - 32, 24)
    print(f"\n0x{addr:X}  value={val:.3f} (delta {delta:.2f}s)")
    for k in range(0, len(ctx), 8):
        print(f"  {k*4-32:+5d} " + "  ".join(f"{v:11.4f}" for v in ctx[k:k + 8]))
memscan.k32.CloseHandle(h)
