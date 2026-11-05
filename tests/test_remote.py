"""In-game controller: mailbox protocol, agent, and the Lua addon itself.

The addon test runs AZSRemote.lua in a real Lua 5.1 (lupa) against a small fake
of the WoW frame API, and wires it to the Python agent through the same table,
so a click in the panel travels the whole way without the game. It is skipped
when lupa is missing:

  uv run --no-project --with numpy --with lupa python -m unittest discover -s tests
"""
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import agent             # noqa: E402
import addon_mailbox as mb  # noqa: E402
import memscan           # noqa: E402
from session import FIELDS   # noqa: E402

LUA_FILE = os.path.join(ROOT, "addon", "AZSRemote", "AZSRemote.lua")


def lua_source():
    with open(LUA_FILE, encoding="utf-8") as f:
        return f.read()


class ProtocolAgreementTest(unittest.TestCase):
    """The Lua file and the Python modules must describe the same mailbox."""

    def test_slots_and_commands_match(self):
        src = lua_source()
        slots = dict(re.findall(r"(\w+) = (\d+)", re.search(r"local SLOT = \{(.*?)\};", src, re.S).group(1)))
        for name in ("REQ", "CMD", "ARG1", "ARG2", "ACK", "RESULT", "HEARTBEAT", "STATE",
                     "VALUES", "LOCKED", "SIZE"):
            self.assertEqual(int(slots[name]), getattr(mb, name), name)
        cmds = dict(re.findall(r"(\w+) = (\d+)", re.search(r"local CMD = \{(.*?)\};", src, re.S).group(1)))
        for name in ("WRITE", "LOCK", "UNLOCK", "HEAL", "UNLOCK_ALL", "RELOCATE"):
            self.assertEqual(int(cmds[name]), getattr(mb, name), name)
        magics = [float(m) for m in re.findall(r'AZSRemote_MB\[\d\] = tonumber\("([-\d.]+)"\)', src)]
        self.assertEqual(tuple(magics), mb.MAGIC)

    def test_field_list_matches_session(self):
        rows = re.findall(r'\{ "(\w+)", "(\w+)", "([^"]+)", "([if])", (true|false) \}', lua_source())
        expected = [(k, n, label, "i" if fmt == "<i" else "f", "true" if lock else "false")
                    for k, n, label, fmt, lock in FIELDS]
        self.assertEqual(rows, expected)
        self.assertEqual(len(FIELDS), mb.N_FIELDS)


class FakeMem:
    def __init__(self, base, size):
        self.base, self.buf = base, bytearray(size)

    def regions(self, h, skip_image=True):
        yield self.base, len(self.buf), 4, 0x20000

    def read(self, h, addr, size):
        o = addr - self.base
        return bytes(self.buf[o:o + size]) if 0 <= o < len(self.buf) else None

    def write(self, h, addr, data):
        o = addr - self.base
        self.buf[o:o + len(data)] = data
        return True

    def patch(self):
        return mock.patch.multiple(memscan, regions=self.regions, read=self.read,
                                   write=self.write, CHUNK=0x1000)


class MailboxFindTest(unittest.TestCase):
    def test_finds_the_array_not_stray_copies(self):
        mem = FakeMem(0x10000, 0x4000)
        m = [struct.pack("<d", v) for v in mb.MAGIC]
        mem.buf[0x100:0x108] = m[0]                     # a stray copy (a local variable)
        base, stride = 0x10000 + 0xFF8, 16              # straddles a 0x1000 chunk edge
        for k in range(3):
            o = base - mem.base + k * stride
            mem.buf[o:o + 8] = m[k]
        with mem.patch():
            box = mb.find(None)
            self.assertEqual((box.base, box.stride), (base, stride))
            self.assertTrue(box.valid())
            box.set(mb.HEARTBEAT, 7)
            self.assertEqual(box.get(mb.HEARTBEAT), 7.0)
            mem.buf[base - mem.base:base - mem.base + 8] = bytes(8)   # UI reloaded
            self.assertFalse(box.valid())


class FakeSession:
    def __init__(self):
        self.connected = True
        self.pins = {}
        self.writes = []
        self.values = {(k, n): 0.0 for k, n, *_ in FIELDS}

    def read(self):
        return dict(self.values)

    def pinned(self):
        return set(self.pins)

    def pin(self, kind, name, value):
        if value is None:
            self.pins.pop((kind, name), None)
            return None
        self.pins[(kind, name)] = float(value)
        return float(value)

    def write(self, kind, name, value):
        self.writes.append((kind, name, float(value)))
        self.values[(kind, name)] = float(value)
        return float(value)

    def heal(self):
        return 500.0

    def close(self):
        pass


class HandleTest(unittest.TestCase):
    def test_commands(self):
        s = FakeSession()
        luck = [i for i, f in enumerate(FIELDS, 1) if f[:2] == ("stat", "luck")][0]
        level = [i for i, f in enumerate(FIELDS, 1) if f[:2] == ("player", "level")][0]
        self.assertEqual(agent.handle(s, False, mb.LOCK, luck, 5)[0], mb.NOT_READY)
        self.assertEqual(agent.handle(s, True, mb.LOCK, luck, 5)[0], mb.OK)
        self.assertEqual(s.pins, {("stat", "luck"): 5.0})
        self.assertEqual(agent.handle(s, True, mb.UNLOCK, luck, 0)[0], mb.OK)
        self.assertEqual(s.pins, {})
        self.assertEqual(agent.handle(s, True, mb.LOCK, 99, 5)[0], mb.BAD_INPUT)
        with mock.patch.object(s, "pin", side_effect=ValueError("level cannot be locked")):
            self.assertEqual(agent.handle(s, True, mb.LOCK, level, 30)[0], mb.BAD_INPUT)
        self.assertEqual(agent.handle(s, True, mb.HEAL, 0, 0)[0], mb.OK)
        self.assertEqual(agent.handle(s, False, mb.RELOCATE, 0, 0)[0], mb.OK)


class InstallAddonTest(unittest.TestCase):
    def test_copies_when_missing_or_changed(self):
        dst = os.path.join(tempfile.mkdtemp(), "AddOns", "AZSRemote")
        self.addCleanup(shutil.rmtree, os.path.dirname(os.path.dirname(dst)))
        self.assertTrue(agent.install_addon(dst=dst))
        self.assertFalse(agent.install_addon(dst=dst))              # up to date: no copy
        with open(os.path.join(dst, "AZSRemote.lua"), "a", encoding="utf-8") as f:
            f.write("-- stale\n")
        self.assertTrue(agent.install_addon(dst=dst))


WOW_STUB = r"""
CHAT, FRAMES, NOW = {}, {}, 0
local Obj = {}
Obj.__index = function(t, k)
  return function(self, a, b)
    if k == "SetScript" then self._s[a] = b
    elseif k == "SetChecked" then self._checked = a
    elseif k == "GetChecked" then return self._checked
    elseif k == "SetText" then self._text = a
    elseif k == "GetText" then return self._text or ""
    elseif k == "Show" then self._shown = true
    elseif k == "Hide" then self._shown = false
    elseif k == "IsVisible" then return self._shown
    elseif k == "GetName" then return self._name
    elseif k == "CreateFontString" then return NewObj()
    elseif k == "RegisterEvent" or k == "UnregisterEvent" then return
    end
  end
end
function NewObj(name)
  local o = setmetatable({ _s = {}, _name = name }, Obj)
  if name then _G[name] = o; _G[name .. "Text"] = NewObj() end
  table.insert(FRAMES, o)
  return o
end
function CreateFrame(kind, name, parent, template) return NewObj(name) end
UIParent = NewObj("UIParent")
DEFAULT_CHAT_FRAME = { AddMessage = function(self, m) table.insert(CHAT, m) end }
function getglobal(n) return _G[n] end
function GetTime() return NOW end
SlashCmdList = {}
function Fire(frame, script, a1)
  this, arg1 = frame, a1
  frame._s[script]()
end
function Tick(dt)
  NOW = NOW + dt
  for _, f in ipairs(FRAMES) do
    if f._s.OnUpdate then Fire(f, "OnUpdate", dt) end
  end
end
"""


class LuaBox:
    """mailbox.Mailbox look-alike over the Lua table (no game memory involved)."""

    def __init__(self, table):
        self.t = table

    def get(self, slot):
        return float(self.t[slot])

    def set(self, slot, value):
        self.t[slot] = float(value)
        return True


try:
    from lupa import lua51
except ImportError:              # pragma: no cover
    lua51 = None


@unittest.skipIf(lua51 is None, "lupa not installed")
class AddonEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.L = lua51.LuaRuntime(unpack_returned_tuples=True)
        self.L.execute(WOW_STUB)
        self.L.execute(lua_source())
        self.g = self.L.globals()
        self.mb = self.g.AZSRemote_MB
        self.session = FakeSession()
        self.agent = agent.Agent(session_factory=lambda: self.session)
        self.agent.box = LuaBox(self.mb)
        self.agent.session = self.session
        self.agent.located_ok = True
        self.agent.last_req = 0.0

    def tick(self, dt=0.2):
        self.g.Tick(dt)

    def test_mailbox_layout(self):
        self.assertEqual(tuple(self.mb[i] for i in (1, 2, 3)), mb.MAGIC)
        self.assertEqual(len(list(self.mb.values())), mb.SIZE)

    def test_click_lock_goes_round_trip(self):
        luck = [i for i, f in enumerate(FIELDS, 1) if f[:2] == ("stat", "luck")][0]
        self.g.SlashCmdList["AZSREMOTE"]()                          # /azs
        self.g["AZSRemoteEdit%d" % luck].SetText(self.g["AZSRemoteEdit%d" % luck], "5")
        cb = self.g["AZSRemoteLock%d" % luck]
        cb._checked = 1
        self.g.Fire(cb, "OnClick")
        self.tick()                                                  # Lua posts the request
        self.assertEqual((self.mb[mb.REQ], self.mb[mb.CMD], self.mb[mb.ARG1], self.mb[mb.ARG2]),
                         (1, mb.LOCK, luck, 5))
        self.agent.serve_once()                                      # Python carries it out
        self.assertEqual(self.session.pins, {("stat", "luck"): 5.0})
        self.assertEqual((self.mb[mb.ACK], self.mb[mb.RESULT]), (1, mb.OK))
        self.agent.heartbeat()
        self.agent.snapshot()
        self.tick()                                                  # Lua shows the result
        status = [f for f in self.g.FRAMES.values() if f._text and "已就绪" in str(f._text)]
        self.assertTrue(status, "status line should say ready")
        self.assertEqual(cb._checked, 1)

    def test_player_write_button_and_not_connected(self):
        self.g.SlashCmdList["AZSREMOTE"]()
        self.tick()
        texts = [str(f._text) for f in self.g.FRAMES.values() if f._text]
        self.assertTrue(any("后台未连接" in t for t in texts))
        chest = [i for i, f in enumerate(FIELDS, 1) if f[:2] == ("player", "chest")][0]
        self.g["AZSRemoteEdit%d" % chest].SetText(self.g["AZSRemoteEdit%d" % chest], "1")
        self.g.Fire(self.g["AZSRemoteWrite%d" % chest], "OnClick")
        self.tick()
        self.agent.serve_once()
        self.assertIn(("player", "chest", 1.0), self.session.writes)

    def test_requests_queue_one_at_a_time(self):
        self.g.Fire(self.g.AZSRemoteHeal, "OnClick")
        self.g.Fire(self.g.AZSRemoteUnlock, "OnClick")
        self.tick()
        self.assertEqual((self.mb[mb.REQ], self.mb[mb.CMD]), (1, mb.HEAL))
        self.tick()
        self.assertEqual(self.mb[mb.REQ], 1)                         # waits for the ack
        self.agent.serve_once()
        self.tick()
        self.assertEqual((self.mb[mb.REQ], self.mb[mb.CMD]), (2, mb.UNLOCK_ALL))


if __name__ == "__main__":
    unittest.main()
