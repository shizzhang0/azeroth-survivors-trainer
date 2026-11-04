"""Game-specific glue shared by every tool: find the process, read the HUD feed."""
import ctypes
import re
from ctypes import wintypes

import memscan

WINDOW_TITLE = "Azeroth Survivors"
EXE_NAME = "AzerothSurvivors"

HUD_MARK = b'{"type":"hud"'
HUD_SPAN = 700          # one HUD frame fits in this many bytes
_NUM = re.compile(r'"(\w+)":(-?[0-9.]+)')


def find_pid():
    """Pid of the game: by window title first, then by executable name."""
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u32.EnumWindows.argtypes = [CB, wintypes.LPARAM]
    u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    found = []

    def cb(hwnd, _l):
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(n + 1, 2))
        u32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value == WINDOW_TITLE:
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append(pid.value)
        return True

    u32.EnumWindows(CB(cb), 0)
    if found:
        return found[0]
    procs = memscan.find_pids(EXE_NAME)
    return procs[0][0] if procs else None


def parse_hud(raw):
    """Every HUD frame in a byte buffer, as {key: number} dicts.

    Only numeric fields are kept; the first occurrence of a key wins (a frame's
    window can run into the next frame).
    """
    frames = []
    start = 0
    while True:
        i = raw.find(HUD_MARK, start)
        if i < 0:
            return frames
        start = i + 1
        d = {}
        for k, v in _NUM.findall(raw[i:i + HUD_SPAN].decode("latin-1")):
            if k not in d:
                try:
                    d[k] = float(v)
                except ValueError:
                    pass
        frames.append(d)


def newest_hud(frames, require=()):
    """The live frame: highest run time, ties broken by the larger hpMax.

    Frames missing any key in `require` (e.g. cut off at a region end) are skipped.
    """
    best = None
    for d in frames:
        if any(k not in d for k in require):
            continue
        key = (d.get("time", 0.0), d.get("hpMax", 0.0))
        if best is None or key > best[0]:
            best = (key, d)
    return best[1] if best else None


def read_hud(h, require=()):
    """Newest HUD frame in the process: live numbers the game itself prints."""
    frames = []
    for _addr, raw in memscan.iter_chunks(h, overlap=HUD_SPAN):
        frames.extend(parse_hud(raw))
    return newest_hud(frames, require)
