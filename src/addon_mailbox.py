"""Memory mailbox shared with the in-game AZSRemote addon.

The addon's Lua sandbox has no io/os/require and no LaunchURL inside a run, so
it cannot reach Python. Python can reach it: the addon keeps a fixed-size array
of numbers (AZSRemote_MB), and Lua stores numbers as plain f64 in memory. We
find the array by its three magic numbers sitting at an equal stride, then
read and write its slots in place.

The slot numbers below are 1-based, like the Lua side, and must match
addon/AZSRemote/AZSRemote.lua (a test checks this).
"""
import struct
import time

import memscan

MAX_STRIDE = 64          # bytes between two array slots we accept

# slots (1-based, as in Lua)
REQ = 4                  # Lua: request sequence number, bumped last
CMD = 5                  # Lua: command code
ARG1 = 6                 # Lua: field number (1-based index into session.FIELDS)
ARG2 = 7                 # Lua: value
ACK = 8                  # Python: echoes REQ once handled
RESULT = 9               # Python: outcome of the last request
HEARTBEAT = 10           # Python: bumped about twice a second while connected
STATE = 11               # Python: what the agent is doing
LUA_BEAT = 12            # Lua: bumped ~10x a second while the addon runs
VALUES = 13              # Python: current value of field i at VALUES + i - 1
N_FIELDS = 26
LOCKED = VALUES + N_FIELDS   # Python: 1 if field i is locked, at LOCKED + i - 1
SIZE = LOCKED + N_FIELDS - 1

# Made at runtime in Lua (tonumber / arithmetic), so they exist only in the table.
# The third one is the layout tag: 1234000 + 1000 * PROTOCOL + field count. An
# addon of another version (a run started before an update) has another tag and
# maybe a shorter array: we must not write into it. Bump PROTOCOL with any slot
# change (here and in the Lua file).
PROTOCOL = 2
LAYOUT_BASE = 1234000.0
MAGIC = (918273.645, -556677.889, LAYOUT_BASE + 1000 * PROTOCOL + N_FIELDS)
LUA_SILENT = 5.0         # seconds without LUA_BEAT moving: that array is dead

# commands (CMD)
WRITE, LOCK, UNLOCK, HEAL, UNLOCK_ALL, RELOCATE = 1, 2, 3, 4, 5, 6
# results (RESULT)
OK, NOT_READY, BAD_INPUT, FAILED = 0, 1, 2, 3
# states (STATE)
LOCATING, READY, LOST = 1, 2, 3

_M = [struct.pack("<d", m) for m in MAGIC]


class Mailbox:
    """One found AZSRemote_MB array: slot k lives at base + (k - 1) * stride."""

    def __init__(self, h, base, stride, tag=MAGIC[2]):
        self.h, self.base, self.stride, self.tag = h, base, stride, tag

    @property
    def compatible(self):
        """Same layout as ours? Never read or write slots of one that isn't."""
        return self.tag == MAGIC[2]

    def describe(self):
        if self.tag == 1234567.125:
            return "pre-versioning addon (21 fields)"
        n = self.tag - LAYOUT_BASE
        if 0 < n < 100000 and n == int(n):
            return f"protocol {int(n) // 1000}, {int(n) % 1000} fields"
        return f"unknown tag {self.tag!r}"

    def addr(self, slot):
        return self.base + (slot - 1) * self.stride

    def get(self, slot):
        raw = memscan.read(self.h, self.addr(slot), 8)
        return struct.unpack("<d", raw)[0] if raw and len(raw) == 8 else None

    def set(self, slot, value):
        return memscan.write(self.h, self.addr(slot), struct.pack("<d", float(value)))

    def valid(self):
        """Still our array? (a new run reloads the UI and the array moves)"""
        raw = memscan.read(self.h, self.base, 2 * self.stride + 8)
        if not raw or len(raw) < 2 * self.stride + 8:
            return False
        return all(raw[i * self.stride:i * self.stride + 8] == _M[i] for i in range(3))


def find(h, watch=0.6):
    """The live mailbox, or None. One sweep of the process memory.

    Matches the first two magics at an equal stride and reads the third as the
    layout tag; check .compatible before touching any other slot. Arrays of
    earlier runs linger in freed memory with intact magics, so every match is
    collected: ours beats any other layout, and among ours the one whose
    LUA_BEAT moves within `watch` seconds is the running addon.
    """
    ours, other = [], None
    for addr, raw in memscan.iter_chunks(h, overlap=2 * MAX_STRIDE + 8):
        i = raw.find(_M[0])
        while i >= 0:
            for stride in range(8, MAX_STRIDE + 1, 8):
                j = i + stride
                tag = raw[j + stride:j + stride + 8]
                if raw[j:j + 8] == _M[1] and len(tag) == 8:
                    box = Mailbox(h, addr + i, stride, struct.unpack("<d", tag)[0])
                    if box.compatible:
                        if all(b.base != box.base for b in ours):
                            ours.append(box)
                    else:
                        other = other or box
                    break
            i = raw.find(_M[0], i + 1)
    if not ours:
        return other
    if len(ours) == 1:
        return ours[0]
    first = {b.base: b.get(LUA_BEAT) for b in ours}
    t0 = time.monotonic()
    while time.monotonic() - t0 < watch:
        time.sleep(0.1)
        for b in ours:
            if b.get(LUA_BEAT) != first[b.base]:
                return b
    return ours[-1]          # nothing moved (game paused?): the newest allocation, likely
