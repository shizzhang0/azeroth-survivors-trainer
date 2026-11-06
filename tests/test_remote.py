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
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import agent             # noqa: E402
import addon_mailbox as mb  # noqa: E402
import memscan           # noqa: E402
import preset            # noqa: E402
import session              # noqa: E402
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
        for name in ("REQ", "CMD", "ARG1", "ARG2", "ACK", "RESULT", "HEARTBEAT", "STATE", "LUA_BEAT",
                     "VALUES", "LOCKED", "SIZE"):
            self.assertEqual(int(slots[name]), getattr(mb, name), name)
        cmds = dict(re.findall(r"(\w+) = (\d+)", re.search(r"local CMD = \{(.*?)\};", src, re.S).group(1)))
        for name in ("WRITE", "LOCK", "UNLOCK", "HEAL", "UNLOCK_ALL", "RELOCATE"):
            self.assertEqual(int(cmds[name]), getattr(mb, name), name)
        magics = [float(m) for m in re.findall(r'AZSRemote_MB\[\d\] = tonumber\("([-\d.]+)"\);', src)]
        self.assertEqual(tuple(magics), mb.MAGIC[:2])
        # the layout tag is computed by the addon from its protocol and field list
        self.assertIn('AZSRemote_MB[3] = tonumber("1234000") + 1000 * PROTOCOL + table.getn(FIELDS);', src)
        self.assertEqual(int(re.search(r"local PROTOCOL = (\d+);", src).group(1)), mb.PROTOCOL)
        self.assertEqual(mb.MAGIC[2], mb.LAYOUT_BASE + 1000 * mb.PROTOCOL + mb.N_FIELDS)

    def test_field_list_matches_session(self):
        rows = re.findall(r'\{ "(\w+)", "(\w+)", "([^"]+)", "([if])", (true|false), "([^"]+)", "([^"]+)" \}',
                          lua_source())
        expected = [(k, n, label, "i" if fmt == "<i" else "f", "true" if lock else "false", unit, group)
                    for k, n, label, fmt, lock, unit, group in FIELDS]
        self.assertEqual(rows, expected)
        self.assertEqual(len(FIELDS), mb.N_FIELDS)


class GameUnitsTest(unittest.TestCase):
    """The panels show and take values the way the game's character pane does."""

    def test_shows_like_the_pane(self):
        # raw values and what the v1118 pane printed for them
        for unit, raw, text in [
                ("num", 447.0, "447"), ("+%1", 1.733, "+73%"), ("haste", 0.877, "+12%"),
                ("%.1", 0.2644, "26.4%"), ("%", 2.254, "225%"), ("%", 0.322, "32%"),
                ("+%1", 1.147, "+15%"), ("+int", 5, "+5"), ("%", 0.5, "50%"),
                ("+%1", 1.32, "+32%"), ("num", 30.57, "30.6"), ("num", 33.0, "33"),
                ("+%1", 5.0, "+400%"), ("+%", 10.0, "+1000%"), ("+%", 0.44, "+44%"),
                ("+%", 0.77, "+77%"), ("+%1", 1.0, "+0%"), ("int", 790, "790")]:
            self.assertEqual(session.show(unit, raw), text, (unit, raw))

    def test_parses_what_you_type(self):
        self.assertAlmostEqual(session.parse("+%1", "+100%"), 2.0)          # 伤害 +100%
        self.assertAlmostEqual(session.parse("haste", "30"), 0.7)           # 急速 30% = cd 0.7
        self.assertAlmostEqual(session.parse("+%", "1000"), 10.0)           # 幸运 +1000%
        self.assertAlmostEqual(session.parse("%", "80%"), 0.8)              # 护甲 80%
        self.assertEqual(session.parse("+int", "+5"), 5)
        self.assertAlmostEqual(session.parse("+%1", "-20"), 0.8)
        with self.assertRaises(ValueError):
            session.parse("%", "abc")

    def test_round_trip(self):
        for unit in ("num", "%", "%.1", "+%", "+%1", "haste"):
            for raw in (0.0, 0.25, 1.0, 1.5, 3.0):
                self.assertAlmostEqual(session.parse(unit, session.to_game(unit, raw)), raw)


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

    def test_current_addon_wins_over_a_stale_older_one(self):
        # the last run's array lingers at a lower address; this run's is further on
        mem = FakeMem(0x10000, 0x3000)
        for base, tag in ((0x10100, 1234567.125), (0x12200, mb.MAGIC[2])):
            for k, v in enumerate((mb.MAGIC[0], mb.MAGIC[1], tag)):
                o = base - mem.base + k * 8
                mem.buf[o:o + 8] = struct.pack("<d", v)
        with mem.patch():
            box = mb.find(None)
        self.assertEqual(box.base, 0x12200)
        self.assertTrue(box.compatible)

    def test_live_array_wins_over_a_dead_one_of_the_same_version(self):
        # several runs with the same addon: the earlier arrays keep their magics
        mem = FakeMem(0x10000, 0x3000)
        dead, live = 0x10100, 0x12200
        for base in (dead, live):
            for k, v in enumerate(mb.MAGIC):
                o = base - mem.base + k * 8
                mem.buf[o:o + 8] = struct.pack("<d", v)
        beat_at = live - mem.base + (mb.LUA_BEAT - 1) * 8

        def lua_ticks(_s):
            n = struct.unpack("<d", bytes(mem.buf[beat_at:beat_at + 8]))[0]
            mem.buf[beat_at:beat_at + 8] = struct.pack("<d", n + 1)
        with mem.patch(), mock.patch.object(mb.time, "sleep", side_effect=lua_ticks):
            self.assertEqual(mb.find(None).base, live)

    def test_agent_notices_the_addon_going_quiet(self):
        mem = FakeMem(0x10000, 0x1000)
        with mem.patch():
            a = agent.Agent(session_factory=FakeSession)
            a.box = mb.Mailbox(None, 0x10100, 8)
            a.lua_beat, a.lua_beat_at = a.box.get(mb.LUA_BEAT), 100.0
            self.assertFalse(a.lua_silent(100.0 + mb.LUA_SILENT - 1))
            self.assertTrue(a.lua_silent(100.0 + mb.LUA_SILENT + 1))
            a.box.set(mb.LUA_BEAT, 7)                        # it ticked again
            self.assertFalse(a.lua_silent(100.0 + mb.LUA_SILENT + 2))

    def _two_arrays(self):
        mem = FakeMem(0x10000, 0x3000)
        for base in (0x10100, 0x12200):
            for k, v in enumerate(mb.MAGIC):
                o = base - mem.base + k * 8
                mem.buf[o:o + 8] = struct.pack("<d", v)
        return mem

    def test_quiet_addon_keeps_connection_and_locks(self):
        # game in the background: no array ticks; ours stays, locks stay
        mem = self._two_arrays()
        s = FakeSession()
        s.pin("stat", "luck", 5)
        with mem.patch(), mock.patch.object(mb.time, "sleep"), \
                mock.patch.object(agent.time, "sleep"), mock.patch.object(agent, "log"):
            a = agent.Agent(session_factory=lambda: s)
            a.session, a.located_ok = s, True
            a.box = mb.Mailbox(None, 0x10100, 8)
            a.check_quiet_addon()
            self.assertEqual(a.box.base, 0x10100)
            self.assertEqual(s.pins, {("stat", "luck"): 5.0})

    def test_quiet_addon_switches_to_a_ticking_new_run(self):
        mem = self._two_arrays()
        new_beat = 0x12200 - mem.base + (mb.LUA_BEAT - 1) * 8

        def tick(_s):
            n = struct.unpack("<d", bytes(mem.buf[new_beat:new_beat + 8]))[0]
            mem.buf[new_beat:new_beat + 8] = struct.pack("<d", n + 1)
        with mem.patch(), mock.patch.object(mb.time, "sleep", side_effect=tick), \
                mock.patch.object(agent.time, "sleep", side_effect=tick), mock.patch.object(agent, "log"):
            a = agent.Agent(session_factory=FakeSession)
            a.box = mb.Mailbox(None, 0x10100, 8)
            a.check_quiet_addon()
            self.assertIsNone(a.box)                          # the loop re-attaches to the new one

    def test_older_addon_is_found_but_never_touched(self):
        # a run that started before the addon was updated: 21 fields, old tag
        mem = FakeMem(0x10000, 0x2000)
        base, stride = 0x10100, 8
        for k, v in enumerate((mb.MAGIC[0], mb.MAGIC[1], 1234567.125)):
            mem.buf[base - mem.base + k * stride:base - mem.base + k * stride + 8] = struct.pack("<d", v)
        before = bytes(mem.buf)
        with mem.patch():
            box = mb.find(None)
            self.assertFalse(box.compatible)
            self.assertIn("21 fields", box.describe())
            a = agent.Agent(session_factory=FakeSession)
            a.h = None
            with mock.patch.object(agent, "log") as log:
                self.assertFalse(a.attach())
                self.assertFalse(a.attach())                    # reported once, not every retry
            self.assertIsNone(a.box)
            self.assertEqual(sum("different version" in c.args[0] for c in log.call_args_list), 1)
        self.assertEqual(bytes(mem.buf), before)                # not a byte written


class FakeSession:
    def __init__(self):
        self.connected = True
        self.events = []
        self.h = 1
        self.lost = False
        self.lost_reason = None
        self.relocated = threading.Event()
        self.closed = False
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

    def locate(self):
        return 0x1000

    def relocate(self):
        self.lost = False
        self.relocated.set()
        return 0x1000, 0x2000

    def close(self):
        self.closed = True


class AgentLifecycleTest(unittest.TestCase):
    def test_goes_after_a_lost_hero_and_keeps_locks(self):
        s = FakeSession()
        s.pin("stat", "luck", 5)
        a = agent.Agent(session_factory=lambda: s)
        a.session, a.located_ok = s, True
        a.maybe_relocate(0)                       # nothing lost: nothing happens
        self.assertFalse(s.relocated.is_set())
        s.lost, s.lost_reason, s.connected = True, "0x1000 no longer holds a stat block", False
        self.assertEqual(a.state(), mb.LOST)
        a.maybe_relocate(0)
        self.assertTrue(s.relocated.wait(2))
        self.assertEqual(s.pins, {("stat", "luck"): 5.0})     # the same session: locks kept

    def test_waits_for_the_game_after_it_closes(self):
        s = FakeSession()
        a = agent.Agent(session_factory=lambda: s)
        a.session, a.pid, a.box, a.located_ok = s, 1234, object(), True
        with mock.patch.object(memscan.k32, "CloseHandle"):
            a.game_gone()
        self.assertTrue(s.closed)
        self.assertEqual((a.pid, a.box, a.session, a.located_ok), (None, None, None, False))


class PresetTest(unittest.TestCase):
    def test_labels_are_unique(self):
        labels = [f[2] for f in FIELDS]
        self.assertEqual(len(labels), len(set(labels)))

    def test_reads_like_the_panel(self):
        got = preset.parse_text("""
            # comment
            伤害 = +100%
            幸运=1000          # inline comment
            暴击：50
            持续伤害层数 = 4
            player.gold = 99999
        """)
        self.assertEqual(got[0], ("stat", "dmg", 2.0))
        self.assertAlmostEqual(got[1][2], 10.0)
        self.assertAlmostEqual(got[2][2], 0.5)
        self.assertEqual(got[3], ("stat", "dots", 3.0))
        self.assertEqual(got[4], ("player", "gold", 99999))

    def test_bad_lines_name_their_line(self):
        for text, msg in [("伤害 100", "第 1 行.*名字 = 数值"),
                          ("\n攻击力 = 5", "第 2 行.*没有叫“攻击力”"),
                          ("金币数量 = 1.5", "第 1 行.*金币数量"),
                          ("伤害 = 10\n伤害 = 20", "第 2 行.*第 1 行已经写过")]:
            with self.assertRaisesRegex(preset.PresetError, msg):
                preset.parse_text(text)

    def test_template_lists_everything_and_does_nothing(self):
        t = preset.template()
        self.assertEqual(preset.parse_text(t), [])
        labels = {f[2] for f in FIELDS}
        uncommented = "\n".join(line[2:] if line[2:].split(" =")[0] in labels else line
                                 for line in t.splitlines())     # every field row, as is
        self.assertEqual(preset.parse_text(uncommented), [])     # "伤害 =" left empty: skipped
        for f in FIELDS:
            self.assertIn(f"# {f[2]} = ", t)

    def test_locks_what_it_can_and_writes_the_rest(self):
        s = FakeSession()
        lines = preset.apply(s, preset.parse_text("幸运 = 500\n等级 = 30\n宝箱价格 = 1"))
        self.assertEqual(set(s.pins), {("stat", "luck"), ("player", "chest")})
        self.assertIn(("player", "level", 30.0), s.writes)
        self.assertEqual(len(lines), 3)

    def test_counters_and_gold_are_written_once_not_locked(self):
        # a locked banish count let a run banish its card pool empty (v1118)
        s = FakeSession()
        preset.apply(s, preset.parse_text("金币数量 = 99999\n重抽次数 = 100\n放逐次数 = 100"))
        self.assertEqual(s.pins, {})
        self.assertIn(("player", "banishes", 100.0), s.writes)
        for name in ("gold", "rerolls", "banishes"):
            with self.assertRaises(ValueError):
                session.Session().pin("player", name, 1)

    def test_one_failure_does_not_stop_the_rest(self):
        s = FakeSession()
        real_pin = s.pin

        def pin(kind, name, value):
            if name == "thorns" and value is not None:
                raise session.SessionError("no slot")
            return real_pin(kind, name, value)
        s.pin = pin
        lines = preset.apply(s, preset.parse_text("荆棘 = 40\n幸运 = 500"))
        self.assertIn("failed", lines[0])
        self.assertIn(("stat", "luck"), s.pins)

    def test_agent_applies_it_after_each_locate(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, preset.FILENAME)
        with open(path, "w", encoding="utf-8") as f:
            f.write("幸运 = 1000\n")
        made = []

        def factory():
            made.append(FakeSession())
            return made[-1]
        a = agent.Agent(session_factory=factory, preset_path=path)
        with mock.patch.object(agent, "log"):
            for run in range(2):                         # a new run: a new session, again
                a.start_locate()
                for _ in range(100):
                    if not a.locating:
                        break
                    threading.Event().wait(0.01)
                self.assertAlmostEqual(made[run].pins[("stat", "luck")], 10.0)
            with open(path, "w", encoding="utf-8") as f:
                f.write("幸运 = oops\n")                 # a broken edit: nothing, no crash
            a.start_locate()
            for _ in range(100):
                if not a.locating:
                    break
                threading.Event().wait(0.01)
        self.assertEqual(made[-1].pins, {})

    def test_missing_file_is_written_commented_out(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, preset.FILENAME)
        self.assertEqual(preset.load(path), [])
        self.assertTrue(preset.ensure_file(path))
        self.assertFalse(preset.ensure_file(path))       # never overwrites yours
        self.assertEqual(preset.load(path), [])


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

    def test_lua_units_agree_with_python(self):
        show_lua, parse_lua = self.g.AZSRemote_Show, self.g.AZSRemote_Parse
        for unit in ("num", "int", "+int", "%", "%.1", "+%", "+%1", "haste", "dots"):
            for raw in (0.0, 0.2644, 0.322, 0.5, 0.877, 1.0, 1.147, 1.733, 2.254, 5, 10, 30.57, 447):
                if unit in ("int", "+int") and raw != int(raw):
                    continue
                self.assertEqual(show_lua(unit, raw), session.show(unit, raw), (unit, raw))
            for text in ("50", "+50%", "-10", "26.4%", "1000"):
                self.assertAlmostEqual(parse_lua(unit, text), session.parse(unit, text), msg=(unit, text))
        self.assertIsNone(parse_lua("%", "abc"))

    def test_panel_shows_game_units_and_sends_raw(self):
        haste = [i for i, f in enumerate(FIELDS, 1) if f[:2] == ("stat", "cd")][0]
        self.g.SlashCmdList["AZSREMOTE"]()
        eb = self.g["AZSRemoteEdit%d" % haste]
        eb.SetText(eb, "+30%")                                       # 急速 +30%
        self.g.Fire(eb, "OnEnterPressed")
        self.tick()
        self.assertAlmostEqual(self.mb[mb.ARG2], 0.7)                # cooldown multiplier
        self.agent.serve_once()
        self.agent.heartbeat()
        self.agent.snapshot()
        self.tick()
        texts = [str(f._text) for f in self.g.FRAMES.values() if f._text]
        self.assertIn("+30%", texts)                                 # shown back as the game would
        self.assertIn("冒险属性", "".join(texts))                     # grouped like the pane

    def test_mailbox_layout(self):
        self.assertEqual(tuple(self.mb[i] for i in (1, 2, 3)), mb.MAGIC)
        self.assertEqual(len(list(self.mb.values())), mb.SIZE)
        before = self.mb[mb.LUA_BEAT]
        self.tick()
        self.tick()
        self.assertEqual(self.mb[mb.LUA_BEAT], before + 2)          # the addon's own heartbeat

    def test_click_lock_goes_round_trip(self):
        luck = [i for i, f in enumerate(FIELDS, 1) if f[:2] == ("stat", "luck")][0]
        self.g.SlashCmdList["AZSREMOTE"]()                          # /azs
        self.g["AZSRemoteEdit%d" % luck].SetText(self.g["AZSRemoteEdit%d" % luck], "+500%")   # 幸运 +500% = raw 5
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

    def test_floating_button_toggles_and_shows_agent_state(self):
        btn, panel = self.g.AZSRemoteToggle, self.g.AZSRemoteFrame
        self.assertFalse(panel._shown)
        self.g.Fire(btn, "OnClick")
        self.assertTrue(panel._shown)
        self.g.Fire(btn, "OnClick")
        self.assertFalse(panel._shown)
        self.g.Fire(btn, "OnDragStart")                          # a drag ...
        self.g.Fire(btn, "OnDragStop")
        self.g.Fire(btn, "OnClick")                              # ... ends in a release, not a click
        self.assertFalse(panel._shown)
        self.g.Fire(btn, "OnClick")
        self.assertTrue(panel._shown)

        self.tick()
        self.assertTrue(str(btn._text).startswith("|cffff5555"))      # red: no agent
        self.agent.heartbeat()
        self.agent.snapshot()
        self.tick()
        self.assertTrue(str(btn._text).startswith("|cff55ff55"))      # green: ready

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
