"""Pin one stat field, but only ever write to a block that passes a strict gate.

Gate: cm==2.00, cnt(int, +52)==0..20, 1.0<=gold<=3.0, 0<=arm<=1.0,
      1.0<=xp<=2.0, hp>100, -0.1<=luck<=50.
That shape is specific to the run-stat struct; anything else is left alone.
"""
import struct
import sys
import time

import numpy as np

import memscan

pid = int(sys.argv[1])
field_off = int(sys.argv[2])
value = float(sys.argv[3])
seed = [int(x, 0) for x in sys.argv[4:]] or []

h = memscan.open_process(pid, write=True)
print(f"pid={pid} pin +{field_off}={value}", flush=True)


def f32(addr):
    return float(memscan.floats_at(h, addr, 1)[0])


def i32(addr):
    raw = memscan.read(h, addr, 4)
    return struct.unpack("<i", raw)[0] if raw and len(raw) == 4 else 0


def gate(addr):
    try:
        hp = f32(addr)
        if not (100 < hp < 200000):
            return False
        if abs(f32(addr + 16) - 2.0) > 0.40:         # cm (2.0..3.4 once cards land)
            return False
        # v0.2.0-1033 layout: arm +32, reg +40, area +44, luck +48, gold +52, cnt +56
        if not (1.0 <= f32(addr + 52) <= 5.0):       # gold multiplier
            return False
        if not (0.0 <= f32(addr + 32) <= 1.0):       # armor
            return False
        if not (1.0 <= f32(addr + 28) <= 3.0):       # xp multiplier
            return False
        if not (-0.1 <= f32(addr + 48) <= 50):       # luck
            return False
        if not (0.5 <= f32(addr + 44) <= 8.0):       # area
            return False
        return True
    except Exception:
        return False


targets = [a for a in seed if gate(a)]
print(f"seeded targets passing the gate: {[hex(t) for t in targets]}", flush=True)

last = 0.0
while True:
    try:
        if not targets or time.time() - last > 30:
            # re-validate; drop anything that no longer looks like the struct
            targets = [a for a in targets if gate(a)]
            if not targets:
                print("no valid target; idle (not writing)", flush=True)
            last = time.time()
        for t in targets:
            if gate(t):
                memscan.write(h, t + field_off, np.float32(value).tobytes())
    except Exception as e:
        print("warn:", e, flush=True)
    time.sleep(0.3)
