"""Read/scan/write another process' memory (x64, same user).

Usage:
  python memscan.py list --name Azeroth
  python memscan.py scan --pid 1234 --anchor 1234.0 --expect 1.25 1.1 0.0
  python memscan.py dump --pid 1234 --addr 0x1234abcd --count 64
  python memscan.py write --pid 1234 --addr 0x1234abcd --value 5.0
"""
import argparse
import ctypes
import struct
import sys
from ctypes import wintypes

import numpy as np

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008

MEM_COMMIT = 0x1000
MEM_IMAGE = 0x1000000
MEM_MAPPED = 0x40000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01

CHUNK = 1 << 24  # 16 MB


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("_pad0", wintypes.WORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("_pad1", wintypes.DWORD),
    ]


k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                               ctypes.POINTER(MBI), ctypes.c_size_t]
k32.ReadProcessMemory.restype = wintypes.BOOL
k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.restype = wintypes.BOOL
k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                               wintypes.DWORD, wintypes.DWORD]
MEM_COMMIT_RESERVE, PAGE_READWRITE = 0x3000, 0x04


def open_process(pid, write=False):
    access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
    if write:
        access |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION
    h = k32.OpenProcess(access, False, pid)
    if not h:
        err = ctypes.get_last_error()
        raise OSError(err, f"OpenProcess({pid}) failed: {ctypes.FormatError(err)}")
    return h


def regions(h, skip_image=True):
    """Yield (base, size, protect, type) for committed readable regions."""
    addr = 0
    mbi = MBI()
    while addr < 0x7FFF_FFFF_FFFF:
        got = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                 ctypes.sizeof(mbi))
        if not got:
            break
        base = mbi.BaseAddress
        size = mbi.RegionSize
        if size <= 0:
            break
        if (mbi.State == MEM_COMMIT
                and not (mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS))
                and not (skip_image and mbi.Type == MEM_IMAGE)):
            yield base, size, mbi.Protect, mbi.Type
        addr = base + size


def iter_chunks(h, overlap=0, chunk=None):
    """Yield (addr, bytes) over every readable region, CHUNK bytes at a time.

    `overlap` extra bytes are read past each chunk (never past the region), so a
    pattern or struct that straddles a chunk boundary is still seen whole. Hits in
    the overlap show up twice — callers dedupe by address.
    """
    chunk = chunk or CHUNK
    for base, size, _prot, _typ in regions(h):
        off = 0
        while off < size:
            n = min(chunk + overlap, size - off)
            raw = read(h, base + off, n)
            if raw:
                yield base + off, raw
            off += chunk


def as_f32(raw):
    """bytes -> float32 view (trailing partial word dropped)."""
    return np.frombuffer(raw, dtype=np.float32, count=len(raw) // 4)


def match_fields(arr, checks):
    """Indices i where lo <= arr[i + k] <= hi for every (k, lo, hi) in checks.

    k is a float index (byte offset // 4). Indices whose struct would run past
    the end of arr are not returned. NaN never matches.
    """
    if not checks:
        return np.empty(0, dtype=np.intp)
    span = max(k for k, _lo, _hi in checks) + 1
    n = len(arr) - span + 1
    if n <= 0:
        return np.empty(0, dtype=np.intp)
    k0, lo0, hi0 = checks[0]
    v = arr[k0:k0 + n]
    idx = np.flatnonzero((v >= lo0) & (v <= hi0))
    for k, lo, hi in checks[1:]:
        if not len(idx):
            break
        v = arr[idx + k]
        idx = idx[(v >= lo) & (v <= hi)]
    return idx


def read(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
        return None
    n = got.value
    return buf.raw[:n] if n else None


def write(h, addr, data):
    buf = ctypes.create_string_buffer(data, len(data))
    got = ctypes.c_size_t(0)
    ok = k32.WriteProcessMemory(h, ctypes.c_void_p(addr), buf, len(data),
                                ctypes.byref(got))
    return bool(ok) and got.value == len(data)


def alloc(h, size=0x1000):
    """Commit a zeroed read/write block in the process (needs a write handle).
    Returns its address or None. Never freed: the game may still point at it."""
    return k32.VirtualAllocEx(h, None, size, MEM_COMMIT_RESERVE, PAGE_READWRITE) or None


def floats_at(h, addr, count):
    raw = read(h, addr - addr % 4, count * 4)
    if raw is None:
        return np.array([], dtype=np.float32)
    return np.frombuffer(raw, dtype=np.float32)


def find_pids(name_part):
    """All pids whose executable name contains name_part (case-insensitive)."""
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    arr = (wintypes.DWORD * 8192)()
    needed = wintypes.DWORD(0)
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        raise OSError(ctypes.get_last_error(), "EnumProcesses failed")
    out = []
    buf = ctypes.create_unicode_buffer(260)
    for pid in arr[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if not pid:
            continue
        h = k32.OpenProcess(0x1000 | PROCESS_VM_READ, False, pid)
        if not h:
            continue
        try:
            if psapi.GetModuleBaseNameW(h, None, buf, 260):
                if name_part.lower() in buf.value.lower():
                    out.append((pid, buf.value))
        finally:
            k32.CloseHandle(h)
    return out


def cmd_list(args):
    for pid, name in find_pids(args.name or ""):
        try:
            h = open_process(pid)
        except OSError as e:
            print(f"{name:32} pid={pid:>6}  (no access: {e.winerror})")
            continue
        total = sum(sz for _, sz, _, _ in regions(h))
        print(f"{name:32} pid={pid:>6}  private_rw={total/1048576:.1f} MB")
        k32.CloseHandle(h)


def cmd_scan(args):
    h = open_process(args.pid)
    anchor = np.float32(args.anchor)
    expects = [np.float32(v) for v in args.expect]
    window = args.window
    hits = []
    scanned = 0
    for addr, raw in iter_chunks(h):
        scanned += len(raw)
        arr = as_f32(raw)
        idx = np.flatnonzero(np.isclose(arr, anchor, rtol=1e-5, atol=1e-7))
        for i in idx:
            a = addr + int(i) * 4
            if any(abs(a - b) < window for b in hits):
                continue
            near = floats_at(h, a - window, (2 * window) // 4)
            found = [bool(np.any(np.isclose(near, e, rtol=1e-5, atol=1e-7)))
                     for e in expects]
            if all(found):
                hits.append(a)
    print(f"scanned {scanned/1048576:.0f} MB, {len(hits)} candidate(s)")
    for a in hits[:args.limit]:
        ctx = floats_at(h, a - 64, 64)
        print(f"\n=== 0x{a:X} ===")
        for k in range(0, len(ctx), 8):
            print("  ", "  ".join(f"{v:10.3f}" for v in ctx[k:k + 8]))
    k32.CloseHandle(h)


def cmd_strscan(args):
    h = open_process(args.pid)
    needle = args.pattern.encode()
    seen = []
    scanned = 0
    for addr, raw in iter_chunks(h, overlap=max(0, len(needle) - 1)):
        scanned += len(raw)
        start = 0
        while True:
            i = raw.find(needle, start)
            if i < 0:
                break
            a = addr + i
            if not any(abs(a - b) < 64 for b in seen):
                seen.append(a)
            start = i + 1
    print(f"scanned {scanned/1048576:.0f} MB, {len(seen)} hit(s)")
    for a in seen[:args.limit]:
        lo = max(0, a - args.context)
        raw = read(h, lo, args.context * 2)
        if not raw:
            continue
        text = "".join(chr(c) if 32 <= c < 127 else "." for c in raw)
        print(f"\n=== 0x{a:X} (offset {a - lo}) ===")
        print(text)
    k32.CloseHandle(h)


def cmd_dump(args):
    h = open_process(args.pid)
    ctx = floats_at(h, args.addr, args.count)
    print(f"0x{args.addr:X}:")
    for k in range(0, len(ctx), 8):
        print(f"  0x{args.addr + k*4:X}  " +
              "  ".join(f"{v:12.4f}" for v in ctx[k:k + 8]))
    k32.CloseHandle(h)


def cmd_write(args):
    h = open_process(args.pid, write=True)
    old = floats_at(h, args.addr, 1)
    ok = write(h, args.addr, struct.pack("<f", args.value))
    new = floats_at(h, args.addr, 1)
    print(f"before={old[0] if len(old) else '?'} write_ok={ok} after={new[0] if len(new) else '?'}")
    k32.CloseHandle(h)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list")
    pl.add_argument("--name", default="")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("scan")
    ps.add_argument("--pid", type=int, required=True)
    ps.add_argument("--anchor", type=float, required=True)
    ps.add_argument("--expect", type=float, nargs="*", default=[])
    ps.add_argument("--window", type=int, default=256)
    ps.add_argument("--limit", type=int, default=8)
    ps.set_defaults(func=cmd_scan)

    pss = sub.add_parser("strscan")
    pss.add_argument("--pid", type=int, required=True)
    pss.add_argument("--pattern", required=True)
    pss.add_argument("--context", type=int, default=300)
    pss.add_argument("--limit", type=int, default=6)
    pss.set_defaults(func=cmd_strscan)

    pd = sub.add_parser("dump")
    pd.add_argument("--pid", type=int, required=True)
    pd.add_argument("--addr", type=lambda s: int(s, 0), required=True)
    pd.add_argument("--count", type=int, default=48)
    pd.set_defaults(func=cmd_dump)

    pw = sub.add_parser("write")
    pw.add_argument("--pid", type=int, required=True)
    pw.add_argument("--addr", type=lambda s: int(s, 0), required=True)
    pw.add_argument("--value", type=float, required=True)
    pw.set_defaults(func=cmd_write)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
