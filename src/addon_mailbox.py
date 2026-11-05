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

import memscan

# created at runtime in Lua with tonumber("..."), so they exist only in the table
MAGIC = (918273.645, -556677.889, 1234567.125)
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
VALUES = 12              # Python: current value of field i at VALUES + i - 1
N_FIELDS = 21
LOCKED = VALUES + N_FIELDS   # Python: 1 if field i is locked, at LOCKED + i - 1
SIZE = LOCKED + N_FIELDS - 1

# commands (CMD)
WRITE, LOCK, UNLOCK, HEAL, UNLOCK_ALL, RELOCATE = 1, 2, 3, 4, 5, 6
# results (RESULT)
OK, NOT_READY, BAD_INPUT, FAILED = 0, 1, 2, 3
# states (STATE)
LOCATING, READY, LOST = 1, 2, 3

_M = [struct.pack("<d", m) for m in MAGIC]


class Mailbox:
    """One found AZSRemote_MB array: slot k lives at base + (k - 1) * stride."""

    def __init__(self, h, base, stride):
        self.h, self.base, self.stride = h, base, stride

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


def find(h):
    """The live mailbox, or None. One sweep of the process memory."""
    for addr, raw in memscan.iter_chunks(h, overlap=2 * MAX_STRIDE + 8):
        i = raw.find(_M[0])
        while i >= 0:
            for stride in range(8, MAX_STRIDE + 1, 8):
                j = i + stride
                if raw[j:j + 8] == _M[1] and raw[j + stride:j + stride + 8] == _M[2]:
                    return Mailbox(h, addr + i, stride)
            i = raw.find(_M[0], i + 1)
    return None
