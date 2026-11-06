"""Game-specific glue shared by every tool: find the process, read the HUD feed."""
import ctypes
import re
import sys
import time
from ctypes import wintypes

import memscan

WINDOW_TITLE = "Azeroth Survivors"
EXE_NAME = "AzerothSurvivors"

HUD_MARK = b'{"type":"hud"'
HUD_SPAN = 700          # one HUD frame fits in this many bytes
_NUM = re.compile(r'"(\w+)":(-?[0-9.]+)')


def utf8_console():
    """Every tool calls this first. Labels and errors are Chinese, and stdout
    piped or redirected on Windows uses the ANSI code page (cp1252, cp437),
    which can't encode them: one print would raise and end the tool."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass            # None under pythonw, or not a stream we can switch


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


HUD_WATCH = 0.8         # seconds to wait before re-reading frames to see which are live
RUN_GAP = 60.0          # frames further apart in run time than this belong to different runs


def dominant_run(frames):
    """Frames of the run with the most frames left in memory (the current one,
    normally): a lone frame of an earlier, longer run must not win on time."""
    timed = sorted((f for f in frames if "time" in f), key=lambda f: f["time"])
    groups, cur = [], []
    for f in timed:
        if cur and f["time"] - cur[-1]["time"] > RUN_GAP:
            groups.append(cur)
            cur = []
        cur.append(f)
    if cur:
        groups.append(cur)
    # most frames wins; on a tie, the later run
    return max(groups, key=lambda g: (len(g), g[-1]["time"])) if groups else frames


def read_hud(h, require=(), watch=None):
    """The current run's newest HUD frame: live numbers the game itself prints.

    Frames of earlier runs linger in memory, and one from a longer run beats the
    current run on `time` (v1118: a level-49 frame at 951 s next to a 116 s run).
    The live run keeps rewriting its frame buffers, so we look again after
    `watch` seconds and take the newest frame among those that changed. If none
    changed (game paused), the run with the most frames in memory wins.
    """
    watch = HUD_WATCH if watch is None else watch
    frames, spots = [], []
    for addr, raw in memscan.iter_chunks(h, overlap=HUD_SPAN):
        i = raw.find(HUD_MARK)
        while i >= 0:
            got = parse_hud(raw[i:i + HUD_SPAN])
            if got:
                frames.append(got[0])
                spots.append((addr + i, got[0]))
            i = raw.find(HUD_MARK, i + 1)
    if watch > 0 and spots:
        time.sleep(watch)
        live = []
        for addr, before in spots:
            raw = memscan.read(h, addr, HUD_SPAN)
            got = parse_hud(raw) if raw else []
            if got and got[0] != before:
                live.append(got[0])
        best = newest_hud(live, require)
        if best:
            return best
    return newest_hud(dominant_run(frames), require) or newest_hud(frames, require)
