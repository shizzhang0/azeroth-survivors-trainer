"""Offline tests: no game needed. A fake process memory stands in for the real one.

  python -m unittest discover -s tests -v
"""
import contextlib
import hashlib
import hmac
import shutil
import tempfile
import threading
import io
import os
import struct
import subprocess
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import game            # noqa: E402
import memscan         # noqa: E402
import offsets         # noqa: E402
import player          # noqa: E402
import save            # noqa: E402
import session         # noqa: E402
import trainer         # noqa: E402
import verify_layout   # noqa: E402

SMALL_CHUNK = 0x1000   # tiny chunks so structs straddle chunk boundaries
game.HUD_WATCH = 0     # fake memory never changes on its own: don't wait to watch it


class FakeMemory:
    """{base: bytearray} regions, patched in for memscan.regions / memscan.read."""

    def __init__(self):
        self.regions = {}
        self.next_page = 0x7000000

    def _alloc(self, h, size=0x1000):
        page, self.next_page = self.next_page, self.next_page + 0x10000
        return self.add(page, size)

    def add(self, base, size):
        self.regions[base] = bytearray(size)
        return base

    def put(self, addr, data):
        for base, buf in self.regions.items():
            if base <= addr and addr + len(data) <= base + len(buf):
                buf[addr - base:addr - base + len(data)] = data
                return
        raise ValueError(f"0x{addr:X} not mapped")

    def _regions(self, h, skip_image=True):
        for base in sorted(self.regions):
            yield base, len(self.regions[base]), 4, 0x20000

    def _read(self, h, addr, size):
        for base, buf in self.regions.items():
            if base <= addr < base + len(buf):
                out = bytes(buf[addr - base:addr - base + size])
                return out or None
        return None

    def patch(self):
        return mock.patch.multiple(memscan, regions=self._regions, read=self._read,
                                   alloc=self._alloc, CHUNK=SMALL_CHUNK)


def stat_block(**over):
    """A 1036-layout stat block, early-run values (zero armor, regen and luck)."""
    vals = dict(hp=1200.0, dmg=1.5, cd=0.9, crit=0.1, cm=2.0, spd=1.1, pick=5.0, xp=1.2,
                arm=0.0, x36=0.6, reg=0.0, area=1.0, luck=0.0, gold=1.0)
    vals.update(over)
    arr = np.zeros(trainer.SPAN, dtype=np.float32)
    for k, v in vals.items():
        arr[offsets.STAT[k] // 4] = v
    return arr.tobytes()


def player_object(level=12, xp=34, hp=900.0, gold=5000, rerolls=3, kills=640, banishes=2,
                  chest=31, maxhp=None, **stats):
    if maxhp is not None:
        stats["hp"] = maxhp             # the stat block's hp slot is max health
    buf = bytearray(player.OBJ_SIZE)
    for name, val in dict(level=level, xp=xp, hp=hp, gold=gold, rerolls=rerolls,
                          kills=kills, banishes=banishes, chest=chest).items():
        off, fmt = offsets.PLAYER[name]
        struct.pack_into(fmt, buf, off, val)
    sb = stat_block(**stats)
    buf[offsets.PLAYER_STAT_BASE:offsets.PLAYER_STAT_BASE + len(sb)] = sb
    return bytes(buf)


HUD_OLD = b'{"type":"hud","time":12.0,"level":2,"hp":100,"hpMax":150,"gold":10,"kills":5}'
HUD_LIVE = (b'{"type":"hud","time":321.5,"level":12,"xp":34,"xpNext":90,"hp":900.0,'
            b'"hpMax":1200,"gold":5000,"kills":640,"rerolls":3}')


class MatchFieldsTest(unittest.TestCase):
    def test_all_checks_must_hold(self):
        arr = np.array([5, 1, 9, 5, 2, 9, 5, 1], dtype=np.float32)
        idx = memscan.match_fields(arr, [(0, 5, 5), (1, 0, 1.5)])
        self.assertEqual(list(idx), [0, 6])

    def test_struct_past_the_end_is_not_returned(self):
        arr = np.array([5, 1, 9, 5], dtype=np.float32)
        self.assertEqual(list(memscan.match_fields(arr, [(0, 5, 5), (2, 9, 9)])), [0])

    def test_nan_never_matches(self):
        arr = np.array([np.nan, 1.0], dtype=np.float32)
        self.assertEqual(list(memscan.match_fields(arr, [(0, -1e9, 1e9)])), [1])


class IterChunksTest(unittest.TestCase):
    def test_overlap_sees_pattern_across_boundary(self):
        mem = FakeMemory()
        base = mem.add(0x10000, 3 * SMALL_CHUNK)
        mem.put(base + SMALL_CHUNK - 3, b"NEEDLE")
        with mem.patch():
            plain = [raw.find(b"NEEDLE") for _a, raw in memscan.iter_chunks(None)]
            lapped = [raw.find(b"NEEDLE") for _a, raw in memscan.iter_chunks(None, overlap=8)]
        self.assertEqual(max(plain), -1)
        self.assertEqual(max(lapped), SMALL_CHUNK - 3)

    def test_overlap_stays_inside_region(self):
        mem = FakeMemory()
        mem.add(0x10000, SMALL_CHUNK + 16)
        with mem.patch():
            sizes = [len(raw) for _a, raw in memscan.iter_chunks(None, overlap=64)]
        self.assertEqual(sizes, [SMALL_CHUNK + 16, 16])


class AliveTest(unittest.TestCase):
    """memscan.alive against a real child process (no game needed)."""

    def test_running_then_exited(self):
        p = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                             stdin=subprocess.PIPE)
        self.addCleanup(p.kill)
        h = memscan.open_process(p.pid)
        self.addCleanup(memscan.k32.CloseHandle, h)
        # a handle without SYNCHRONIZE can't be waited on: the exit code answers
        bare = memscan.k32.OpenProcess(memscan.PROCESS_QUERY_INFORMATION, False, p.pid)
        self.addCleanup(memscan.k32.CloseHandle, bare)
        self.assertTrue(memscan.alive(h))
        self.assertTrue(memscan.alive(bare))
        p.stdin.close()
        p.wait(10)
        self.assertFalse(memscan.alive(h))
        self.assertFalse(memscan.alive(bare))


class HudTest(unittest.TestCase):
    def test_parse_and_pick_newest(self):
        frames = game.parse_hud(b"junk" + HUD_LIVE + b"\0" * 20 + HUD_OLD)
        self.assertEqual(len(frames), 2)
        hud = game.newest_hud(frames)
        self.assertEqual(hud["time"], 321.5)
        self.assertEqual(hud["hpMax"], 1200)
        self.assertEqual(hud["kills"], 640)

    def test_require_skips_cut_frames(self):
        cut = b'{"type":"hud","time":999.0,"level":12'
        frames = game.parse_hud(HUD_LIVE + b"\0" * 800 + cut)
        self.assertEqual(game.newest_hud(frames)["time"], 999.0)
        self.assertEqual(game.newest_hud(frames, require=("gold",))["time"], 321.5)

    def test_read_hud_across_chunk_boundary(self):
        mem = FakeMemory()
        base = mem.add(0x40000, 2 * SMALL_CHUNK)
        mem.put(base + SMALL_CHUNK - 20, HUD_LIVE)
        with mem.patch():
            hud = game.read_hud(None, require=("gold",))
        self.assertEqual(hud["gold"], 5000)

    @staticmethod
    def frame(t, level, kills, gold):
        return (b'{"type":"hud","time":%.1f,"level":%d,"hpMax":500,"gold":%d,"kills":%d}'
                % (t, level, gold, kills)).ljust(200, b"\0")

    def test_live_run_beats_a_longer_earlier_run(self):
        # v1118, many runs in one session: a level-49 frame at 951 s lingered and
        # outranked the current 116 s run on time
        mem = FakeMemory()
        base = mem.add(0x40000, SMALL_CHUNK)
        mem.put(base, self.frame(951.9, 49, 5982, 2935))
        live_at = base + 0x400
        mem.put(live_at, self.frame(116.7, 11, 242, 166))

        def game_moves_on(_s):                  # the live buffer is rewritten meanwhile
            mem.put(live_at, self.frame(117.5, 11, 245, 170))
        with mem.patch(), mock.patch.object(game.time, "sleep", side_effect=game_moves_on):
            hud = game.read_hud(None, require=("gold", "kills", "level"), watch=0.5)
        self.assertEqual((hud["level"], hud["kills"]), (11, 245))

    def test_paused_game_falls_back_to_the_run_with_most_frames(self):
        mem = FakeMemory()
        base = mem.add(0x40000, SMALL_CHUNK)
        mem.put(base, self.frame(951.9, 49, 5982, 2935))
        for k, t in enumerate((110.0, 112.0, 116.7)):
            mem.put(base + 0x200 * (k + 1), self.frame(t, 11, 200 + k, 150))
        with mem.patch():
            hud = game.read_hud(None, require=("gold",), watch=0)
        self.assertEqual(hud["time"], 116.7)


class TrainerTest(unittest.TestCase):
    def setUp(self):
        self.mem = FakeMemory()
        self.base = self.mem.add(0x10000, 4 * SMALL_CHUNK)

    def test_finds_early_run_block_across_chunk_boundary(self):
        # luck=0 / reg=0 at run start: the old gate (stale v977 field order)
        # tested luck against the gold range and rejected this block.
        real = self.base + SMALL_CHUNK - 8
        self.mem.put(real, stat_block())
        self.mem.put(self.base + 3 * SMALL_CHUNK, stat_block(hp=999.0))   # look-alike
        with self.mem.patch():
            self.assertEqual(trainer.find_copies(None, hp_hint=1200.0), [real])
            self.assertEqual(len(trainer.find_copies(None)), 2)

    def test_block_ok_ignores_pinned_fields(self):
        arr = np.frombuffer(stat_block(dmg=500.0, pick=5000.0), dtype=np.float32)
        self.assertFalse(trainer.block_ok(arr, trainer.stat_checks()))
        self.assertTrue(trainer.block_ok(arr, trainer.stat_checks(skip={"dmg", "pick"})))

    def test_block_ok_rejects_reused_memory(self):
        arr = np.zeros(trainer.SPAN, dtype=np.float32)
        self.assertFalse(trainer.block_ok(arr, trainer.stat_checks(skip={"dmg"})))

    def test_parse_specs(self):
        (name, off, data), = trainer.parse_specs(["luck=5"])
        self.assertEqual((name, off), ("luck", offsets.STAT["luck"]))
        self.assertEqual(np.frombuffer(data, np.float32)[0], 5.0)
        (name, off, data), = trainer.parse_specs(["targets=7"])
        self.assertEqual((name, off), ("targets", 56))
        self.assertEqual(np.frombuffer(data, np.int32)[0], 7)            # i32, not f32
        for bad in ("x36=1", "nope=1", "dmg", "dmg=abc", "targets=1.5"):
            with self.assertRaises(SystemExit, msg=bad):
                trainer.parse_specs([bad])


class PlayerTest(unittest.TestCase):
    def test_locate_by_fingerprint_and_hud(self):
        mem = FakeMemory()
        heap = mem.add(0x100000, 8 * SMALL_CHUNK)
        mem.add(0x200000, SMALL_CHUNK)
        obj = heap + 2 * SMALL_CHUNK - 0x100            # fields straddle a chunk edge
        mem.put(heap + 0x40, player_object(kills=3, gold=1))   # stale hero: fails HUD check
        mem.put(obj, player_object())
        mem.put(0x200000 + 64, HUD_OLD + b"\0" * 8 + HUD_LIVE)
        with mem.patch():
            base, info = player.locate(None)
        self.assertEqual(base, obj)
        self.assertEqual(info["kills"], 640)

    def test_locate_prefers_the_candidate_that_agrees_with_the_hud(self):
        # v1118: a stray block passed the loose gold gate (level 1, max hp 98,
        # kills garbage) and won only because it sat at a lower address.
        mem = FakeMemory()
        heap = mem.add(0x100000, 8 * SMALL_CHUNK)
        mem.add(0x200000, SMALL_CHUNK)
        decoy = heap + 0x40
        real = heap + 4 * SMALL_CHUNK
        mem.put(decoy, player_object(level=1, gold=5600, kills=-2147372288, hp=0.0, maxhp=98.0))
        mem.put(real, player_object())
        mem.put(0x200000 + 64, HUD_LIVE)
        with mem.patch():
            base, _info = player.locate(None)
        self.assertEqual(base, real)
        info = game.parse_hud(HUD_LIVE)[0]
        self.assertEqual(player.hud_score(mem._read(None, real, player.OBJ_SIZE), info), 4)
        self.assertLessEqual(player.hud_score(mem._read(None, decoy, player.OBJ_SIZE), info), 1)

    def test_tie_goes_to_the_one_that_moves(self):
        # v1118, run start: a frozen copy with the same opening numbers sat below
        # the hero, scored the same, and won the tie by address.
        mem = FakeMemory()
        heap = mem.add(0x100000, 8 * SMALL_CHUNK)
        mem.add(0x200000, SMALL_CHUNK)
        frozen, hero = heap + 0x40, heap + 4 * SMALL_CHUNK
        mem.put(frozen, player_object())
        mem.put(hero, player_object())
        mem.put(0x200000 + 64, HUD_LIVE)

        def tick(_s):                      # the hero lands a kill while we watch
            mem.put(hero, player_object(kills=641))
        with mem.patch(), mock.patch.object(player.time, "sleep", side_effect=tick):
            self.assertEqual(player.locate_ranked(None)[0], [frozen, hero])
            base, _info = player.locate(None)
        self.assertEqual(base, hero)

    def test_tie_with_nothing_moving_keeps_the_lowest(self):
        mem = FakeMemory()
        heap = mem.add(0x100000, 8 * SMALL_CHUNK)
        mem.put(heap + 0x40, player_object())
        mem.put(heap + 4 * SMALL_CHUNK, player_object())
        with mem.patch():
            self.assertIsNone(player.pick_live(None, [heap + 0x40, heap + 4 * SMALL_CHUNK],
                                               wait=0.05, step=0.01))

    def test_refuses_to_write_unverified_field(self):
        with mock.patch.object(sys, "argv", ["player.py", "--banishes", "9"]), \
                mock.patch.object(offsets, "PLAYER_UNVERIFIED", {"banishes"}), \
                mock.patch.object(player, "PLAYER_UNVERIFIED", {"banishes"}), \
                mock.patch.object(game, "find_pid") as find_pid:
            with self.assertRaises(SystemExit):
                player.main()
            find_pid.assert_not_called()          # refused before touching the game

    def test_writes_chest_price(self):
        written = {}
        with mock.patch.object(sys, "argv", ["player.py", "--pid", "1", "--chest", "1"]), \
                mock.patch.object(memscan, "open_process", return_value=None), \
                mock.patch.object(memscan.k32, "CloseHandle"), \
                mock.patch.object(player, "locate", return_value=(0x5000, {})), \
                mock.patch.object(player, "show"), \
                mock.patch.object(player.time, "sleep"), \
                mock.patch.object(memscan, "write",
                                  side_effect=lambda _h, a, b: written.update({a: b})),                 contextlib.redirect_stdout(io.StringIO()):
            player.main()
        self.assertEqual(written, {0x5000 + offsets.PLAYER["chest"][0]: struct.pack("<i", 1)})

    def test_matches_hud_rejects_overfull_health(self):
        blob = player_object(hp=5000.0)
        self.assertFalse(player.matches_hud(blob, {"kills": 640, "gold": 5000}))
        self.assertTrue(player.matches_hud(player_object(), {"kills": 640}))


class VerifyLayoutTest(unittest.TestCase):
    def test_newest_layout_that_fits_wins(self):
        mem = FakeMemory()
        base = mem.add(0x10000, 2 * SMALL_CHUNK)
        blk = base + SMALL_CHUNK - 16
        mem.put(blk, stat_block(luck=0.3, gold=1.25, area=1.1, arm=0.05))
        line = dict(hp=1200.0, cm=2.0, arm=0.05, area=1.1, luck=0.3, gold=1.25)
        with mem.patch():
            hits = {name: verify_layout.find_struct(None, line, lay)
                    for name, lay in verify_layout.LAYOUTS.items()}
        self.assertEqual(hits["v0.2.0-1036"], [blk])
        self.assertEqual(hits["v0.2.0-977"], [])

    def test_same_layouts_are_swept_once(self):
        groups = verify_layout.distinct_layouts()
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0][0], ["v0.2.0-1118", "v0.2.0-1115", "v0.2.0-1039",
                                        "v0.2.0-1036", "v0.2.0-1033"])
        self.assertEqual(groups[1][0], ["v0.2.0-977"])
        self.assertEqual(sum(len(n) for n, _l in groups), len(offsets.STAT_LAYOUTS))
        line = dict(hp=1200.0, cm=2.0, arm=0.05, area=1.1, luck=0.3, gold=1.25)
        ctx = np.frombuffer(stat_block(luck=0.3, gold=1.25, area=1.1, arm=0.05), dtype=np.float32)

        def run(found):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["verify_layout.py", "1"]), \
                    mock.patch.object(memscan, "open_process", return_value=7), \
                    mock.patch.object(memscan.k32, "CloseHandle"), \
                    mock.patch.object(memscan, "floats_at", return_value=ctx), \
                    mock.patch.object(verify_layout, "stat_lines", return_value={"t": line}), \
                    mock.patch.object(verify_layout, "verify_player"), \
                    mock.patch.object(verify_layout, "find_struct", return_value=found) as fs, \
                    contextlib.redirect_stdout(out):
                verify_layout.main()
            return fs.call_count, out.getvalue()
        sweeps, _text = run([])
        self.assertEqual(sweeps, 2)                               # was one per version: 6
        sweeps, text = run([0x1000])
        self.assertEqual(sweeps, 1)
        self.assertIn("layout that fits: v0.2.0-1118 / v0.2.0-1115 / v0.2.0-1039 / "
                      "v0.2.0-1036 / v0.2.0-1033", text)
        self.assertIn("VERDICT: offsets still valid", text)

    def test_stale_line_before_a_card_pick_is_not_a_moved_field(self):
        lay = offsets.STAT
        ctx = np.frombuffer(stat_block(luck=0.48, xp=1.69), dtype=np.float32)
        old = dict(hp=1200.0, cm=2.0, luck=0.30, xp=1.2, area=1.0, gold=1.0)
        new = dict(old, luck=0.48, xp=1.69)
        self.assertEqual(verify_layout.compare_line(old, lay, ctx)[1], 2)
        best = min([old, new], key=lambda v: verify_layout.compare_line(v, lay, ctx)[1:])
        self.assertIs(best, new)
        self.assertEqual(verify_layout.compare_line(best, lay, ctx)[1], 0)

    def test_check_player_flags_moved_offset(self):
        hud = {"level": 16, "gold": 399, "kills": 341, "rerolls": 5, "chestCost": 31}
        ok = {f: v for f, _m, _r, v in verify_layout.check_player(
            player_object(level=16, gold=399, kills=341, rerolls=5, hp=486.0), hud)}
        self.assertEqual(ok["kills"], "OK")
        self.assertEqual(ok["level"], "OK")
        self.assertEqual(ok["chest"], "OK")
        # what v1039 looked like through the old kills offset: garbage
        moved = {f: v for f, _m, _r, v in verify_layout.check_player(
            player_object(level=16, gold=399, kills=-696905680, rerolls=5), hud)}
        self.assertEqual(moved["kills"], "MISMATCH")
        self.assertEqual(moved["gold"], "OK")

    def test_stat_lines(self):
        mem = FakeMemory()
        base = mem.add(0x10000, 2 * SMALL_CHUNK)
        text = b"hp=1200;dmg=1.50;cd=0.90;cm=2.00;arm=0.05;area=1.10;luck=0.30;gold=1.25;pet=1.00"
        mem.put(base + 600, text)
        with mem.patch():
            lines = verify_layout.stat_lines(None)
        (v,) = lines.values()
        self.assertEqual(v["luck"], 0.3)
        self.assertEqual(v["pet"], 1.0)


class OffsetsTest(unittest.TestCase):
    def test_tables_agree(self):
        self.assertIs(offsets.STAT, offsets.STAT_LAYOUTS[offsets.GAME_VERSION])
        self.assertLessEqual(set(offsets.STAT_RANGES), set(offsets.STAT))
        self.assertLessEqual(set(offsets.PLAYER_FINGERPRINT), set(offsets.STAT_RANGES))
        self.assertTrue(set(offsets.STAT_TAIL).isdisjoint(offsets.STAT))
        self.assertLessEqual(offsets.PLAYER_UNVERIFIED, set(offsets.PLAYER))

    def test_current_version_is_tried_first(self):
        # verify_layout.py reports the FIRST layout that fits; identical layouts
        # (1118 == 1115 == 1039 == 1036) must name the build we actually target.
        self.assertEqual(next(iter(offsets.STAT_LAYOUTS)), offsets.GAME_VERSION)

    def test_matches_docs(self):
        # docs/OFFSETS.md: object + 0x5F4 = stat block, luck @ 0x624, gold mult @ 0x628
        self.assertEqual(offsets.player_stat("hp"), 0x5F4)
        self.assertEqual(offsets.player_stat("luck"), 0x624)
        self.assertEqual(offsets.player_stat("gold"), 0x628)


class SessionTest(unittest.TestCase):
    """The GUI's core, against a fake player object (no game, no window)."""

    def setUp(self):
        self.mem = FakeMemory()
        self.obj = self.mem.add(0x40000, 0x2000)
        self.mem.put(self.obj, player_object(gold=120, chest=31))
        self.s = session.Session()
        self.s.base = self.obj           # as if locate() had found it
        p = self.mem.patch()
        p.start()
        self.addCleanup(p.stop)
        w = mock.patch.object(memscan, "write",
                              side_effect=lambda _h, a, d: self.mem.put(a, d) or True)
        w.start()
        self.addCleanup(w.stop)

    def test_read_both_layers(self):
        v = self.s.read()
        self.assertEqual(v[("player", "gold")], 120)
        self.assertEqual(v[("player", "chest")], 31)
        self.assertAlmostEqual(v[("stat", "cm")], 2.0)

    def test_write_player_field(self):
        self.assertEqual(self.s.write("player", "chest", "1"), 1)
        self.assertEqual(self.s.read()[("player", "chest")], 1)

    def test_level_stays_within_the_games_cap(self):
        self.assertEqual(self.s.write("player", "level", 120), 120)
        for bad in (121, 999, 0):
            with self.assertRaisesRegex(ValueError, "1-120"):
                self.s.write("player", "level", bad)
        self.assertEqual(self.s.read()[("player", "level")], 120)

    def test_lock_rewrites_after_the_game_recomputes(self):
        self.s.pin("stat", "luck", "5")
        self.s.tick()
        self.assertAlmostEqual(self.s.read()[("stat", "luck")], 5.0)
        self.mem.put(self.obj, player_object(gold=120))         # game recomputed: luck 0
        self.s.tick()
        self.assertAlmostEqual(self.s.read()[("stat", "luck")], 5.0)

    def test_lock_takes_effect_at_once(self):
        self.s.pin("stat", "luck", 5)                            # no tick yet
        self.s.pin("player", "chest", 1)
        v = self.s.read()
        self.assertAlmostEqual(v[("stat", "luck")], 5.0)
        self.assertEqual(v[("player", "chest")], 1)

    def test_lock_that_cannot_write_keeps_the_old_one(self):
        self.s.pin("stat", "luck", 3)
        with mock.patch.object(memscan, "write", return_value=False):
            with self.assertRaises(session.SessionError):
                self.s.pin("stat", "luck", 5)
            with self.assertRaises(session.SessionError):
                self.s.pin("stat", "dmg", 2)
        self.assertEqual(self.s.pinned(), {("stat", "luck")})
        self.s.tick()
        self.assertAlmostEqual(self.s.read()[("stat", "luck")], 3.0)

    def test_effect_lock_writes_once(self):
        self._effect_list([(0x16, 0.3)], cap=4)
        with mock.patch.object(self.s, "_write_effect", wraps=self.s._write_effect) as w:
            self.s.pin("effect", "thorns", 0.5)
        self.assertEqual(w.call_count, 1)
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.5, places=5)

    def test_out_of_range_lock_does_not_kill_the_target(self):
        # luck=500 is outside STAT_RANGES; the liveness check must skip pinned fields
        self.s.pin("stat", "luck", 500)
        self.assertEqual(self.s.tick(), 0)
        self.assertIsNotNone(self.s.read())

    def test_target_gone_counts_misses_and_stops_reading(self):
        self.s.pin("player", "chest", 1)
        self.mem.put(self.obj, bytes(0x2000))                   # memory freed / reused
        self.assertEqual(self.s.tick(0), 1)
        self.assertIsNone(self.s.read())

    def test_follows_the_live_look_alike(self):
        # located on 移动以开始: ours and a twin look the same; once the run starts
        # ours stays frozen and the twin moves -> switch, keeping the locks
        twin = self.mem.add(0x60000, 0x2000)
        self.mem.put(twin, player_object(gold=120, chest=31))
        self.s.alternates = [twin]
        self.s.pin("stat", "luck", 5)
        now = [100.0]
        with mock.patch.object(session.time, "monotonic", side_effect=lambda: now[0]):
            self.assertIsNotNone(self.s.read())                 # first look
            self.mem.put(twin, player_object(gold=125, chest=31, kills=641))
            now[0] += session.FOLLOW_EVERY + 0.1
            self.s.read()
        self.assertEqual(self.s.base, twin)
        self.assertEqual(self.s.alternates, [self.obj])
        self.assertTrue(self.s.events and "switched" in self.s.events[0])
        self.assertIn(("stat", "luck"), self.s.pinned())

    def _stop_thread(self):
        self.s._stop.set()
        if self.s._thread:
            self.s._thread.join(timeout=1)

    def test_lost_keeps_locks_and_says_why(self):
        # the game moved the hero: its old memory stops looking like one
        self.s.h = 1
        self.s.interval = 0.001
        self.s.pin("stat", "luck", 5)                      # also fine with no lock at all
        self.mem.put(self.obj, bytes(0x2000))
        t = threading.Thread(target=self.s._run)
        t.start()
        t.join(timeout=2)
        self.assertTrue(self.s.lost)
        self.assertIn("no longer", self.s.lost_reason)
        self.assertEqual(self.s.pinned(), {("stat", "luck")})

    def test_relocate_follows_the_hero_without_the_hud(self):
        self.s.h = 1
        self.addCleanup(self._stop_thread)
        self.s.pin("stat", "luck", 5)
        self.s.read()                                       # remember level/kills/gold/max hp
        moved = self.mem.add(0x80000, 0x2000)
        self.mem.put(moved, player_object(gold=125, kills=650, chest=31))   # a bit later
        self.mem.put(self.obj, bytes(0x2000))
        self.s.lost = True
        old, new = self.s.relocate()                        # no HUD in this memory at all
        self.assertEqual((old, new), (self.obj, moved))
        self.assertFalse(self.s.lost)
        self.assertEqual(self.s.pinned(), {("stat", "luck")})
        self.assertEqual(self.s.read()[("player", "gold")], 125)

    def test_relocate_without_a_hint_needs_the_hud(self):
        self.s.h = 1
        with self.assertRaisesRegex(session.SessionError, "HUD"):
            self.s.relocate()                               # never read, no HUD frame

    def test_tail_fields_and_integer_targets(self):
        # v1118 pane: +56 is 目标数 (i32), +60 回响, +64 击杀治疗, +68 增益强度, +76 持续伤害急速
        self.assertEqual(self.s.write("stat", "targets", "7"), 7)
        raw = self.mem._read(None, self.obj + offsets.PLAYER_STAT_BASE + 56, 4)
        self.assertEqual(struct.unpack("<i", raw)[0], 7)
        self.s.write("stat", "echo", 0.22)
        v = self.s.read()
        self.assertEqual(v[("stat", "targets")], 7)
        self.assertAlmostEqual(v[("stat", "echo")], 0.22, places=5)
        with self.assertRaises(ValueError):
            self.s.write("stat", "targets", 1.5)

    def _effect_list(self, entries, cap=None):
        """Point the hero's effect Vec at `entries` [(tag, value)] (8 bytes each).
        cap defaults to the length: a full list, no spare slot."""
        heap = self.mem.add(0x90000, 0x1000)
        buf = b"".join(struct.pack("<B3xf", t, v) for t, v in entries)
        self.mem.put(heap, buf)
        self.mem.put(self.obj + offsets.EFFECT_LIST[0], struct.pack("<Q", heap))
        self.mem.put(self.obj + offsets.EFFECT_LIST[1], struct.pack("<Q", len(entries)))
        self.mem.put(self.obj + offsets.EFFECT_CAP,
                     struct.pack("<Q", len(entries) if cap is None else cap))
        return heap

    def _list_len(self):
        return struct.unpack("<Q", self.mem._read(None, self.obj + offsets.EFFECT_LIST[1], 8))[0]

    def _entry(self, heap, i):
        return struct.unpack("<B3xf", self.mem._read(None, heap + 8 * i, 8))

    def test_own_entry_goes_into_a_spare_slot(self):
        # v1118: 7 talents, capacity 8 -> appended (0x16, 0.5) showed 荆棘 50%
        heap = self._effect_list([(0x05, 0.1), (0x16, 0.3)], cap=4)
        self.s.pin("effect", "thorns", 0.77)
        self.assertEqual(self._list_len(), 3)
        tag, value = self._entry(heap, 2)
        self.assertEqual(tag, 0x16)
        self.assertAlmostEqual(value, 0.47, places=5)           # just the difference
        self.assertAlmostEqual(self._entry(heap, 1)[1], 0.3, places=5)   # talent untouched
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.77, places=5)
        self.s.pin("effect", "thorns", 0.9)                       # re-lock: same slot
        self.s.tick()
        self.assertEqual(self._list_len(), 3)
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.9, places=5)
        self.s.pin("effect", "thorns", None)                      # unlock: counted out
        self.assertEqual(self._list_len(), 2)
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.3, places=5)

    def test_thorns_without_any_source_when_there_is_a_slot(self):
        self._effect_list([(0x05, 0.1)], cap=4)
        self.s.write("effect", "thorns", 0.5)
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.5, places=5)

    def test_unlock_leaves_entries_after_ours_alone(self):
        heap = self._effect_list([(0x05, 0.1)], cap=4)
        self.s.pin("effect", "thorns", 0.5)
        self.mem.put(heap + 16, struct.pack("<B3xf", 0x07, 0.2))  # game appends after us
        self.mem.put(self.obj + offsets.EFFECT_LIST[1], struct.pack("<Q", 3))
        self.s.pin("effect", "thorns", None)
        self.assertEqual(self._list_len(), 3)                     # can't count ours out:
        self.assertEqual(self.s.read()[("effect", "thorns")], 0)  # it adds 0 instead
        self.assertAlmostEqual(self._entry(heap, 2)[1], 0.2, places=5)

    def _header(self):
        return struct.unpack("<QQQ", self.mem._read(None, self.obj + offsets.EFFECT_CAP, 24))

    def test_full_list_moves_to_our_page_and_back(self):
        heap = self._effect_list([(0x05, 0.1), (0x16, 0.3)])      # 8 of 8, say
        self.s.pin("effect", "thorns", 0.9)
        cap, ptr, n = self._header()
        self.assertEqual((cap, n), (0, 3))                        # cap 0: never freed by the game
        self.assertNotEqual(ptr, heap)
        self.assertEqual(self._entry(ptr, 0), self._entry(heap, 0))   # a copy...
        self.assertAlmostEqual(self._entry(ptr, 1)[1], 0.3, places=5)  # talent untouched
        self.assertEqual(self._entry(ptr, 2)[0], 0x16)            # ...plus ours
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.9, places=5)
        self.s.pin("effect", "lifesteal", 0.1)                    # second one: same page
        self.assertEqual(self._header()[1:], (ptr, 4))
        self.s.pin("effect", "thorns", None)
        self.assertEqual(self._header()[1], ptr)                  # lifesteal still ours
        self.s.pin("effect", "lifesteal", None)
        self.assertEqual(self._header(), (2, heap, 2))            # the game's buffer again
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.3, places=5)

    def test_no_talent_stats_at_all(self):
        # druid with no talent stats: empty Vec, dangling pointer 4, capacity 0
        self.mem.put(self.obj + offsets.EFFECT_CAP, struct.pack("<QQQ", 0, 4, 0))
        self.s.pin("effect", "thorns", 0.4)
        self.s.pin("effect", "lifesteal", 0.1)
        v = self.s.read()
        self.assertAlmostEqual(v[("effect", "thorns")], 0.4, places=5)
        self.assertAlmostEqual(v[("effect", "lifesteal")], 0.1, places=5)
        self.assertEqual(self._header()[0], 0)
        self.s.pin("effect", "thorns", None)
        self.s.pin("effect", "lifesteal", None)
        self.assertEqual(self._header(), (0, 4, 0))

    def test_new_run_after_a_move_is_left_alone(self):
        self._effect_list([(0x05, 0.1)])
        self.s.pin("effect", "thorns", 0.5)
        fresh = self.mem.add(0x95000, 0x100)                      # the game starts over
        self.mem.put(self.obj + offsets.EFFECT_CAP, struct.pack("<QQQ", 4, fresh, 0))
        with self.s._lock:
            self.s._pins.clear()
        self.s._clear_effect("thorns")
        self.assertEqual(self._header(), (4, fresh, 0))           # not pointed at the old run

    def test_full_list_changes_a_source_when_it_cannot_move(self):
        no_page = mock.patch.object(memscan, "alloc", return_value=None)
        no_page.start()
        self.addCleanup(no_page.stop)
        heap = self._effect_list([(0x05, 0.1), (0x16, 0.3)])
        self.s.pin("effect", "thorns", 0.9)
        self.assertEqual(self._list_len(), 2)
        self.assertAlmostEqual(self._entry(heap, 1)[1], 0.9, places=5)
        self.s.pin("effect", "thorns", None)
        self.assertAlmostEqual(self._entry(heap, 1)[1], 0.3, places=5)

    def test_close_takes_our_entries_out(self):
        self._effect_list([(0x05, 0.1)], cap=4)
        self.s.h = 7                                              # close() needs a handle
        self.s.pin("effect", "thorns", 0.5)
        with mock.patch.object(memscan.k32, "CloseHandle"):
            self.s.close()
        self.s.h, self.s.base = 7, self.obj
        self.assertEqual(self._list_len(), 1)
        self.assertEqual(self.s.read()[("effect", "thorns")], 0)

    def test_lifesteal_own_entry_leaves_the_ring_alone(self):
        self._effect_list([(0x16, 0.3)], cap=4)
        items = self._item_list([(0x12, 0.03)])
        self.s.write("effect", "lifesteal", 0.25)
        self.assertAlmostEqual(self.s.read()[("effect", "lifesteal")], 0.25, places=5)
        ring = struct.unpack("<f", self.mem._read(None, items + offsets.ITEM_VALUE, 4))[0]
        self.assertAlmostEqual(ring, 0.03, places=5)

    def test_thorns_is_the_sum_of_its_effect_entries(self):
        heap = self._effect_list([(0x05, 0.1), (0x16, 0.2), (0x0b, 0.25), (0x16, 0.1)])
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.3, places=5)
        self.s.write("effect", "thorns", 0.77)                   # ours carries the 0.47
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.77, places=5)
        for i, v in enumerate([0.1, 0.2, 0.25, 0.1]):             # the game's entries untouched
            self.assertAlmostEqual(self._entry(heap, i)[1], v, places=5)

    def test_thorns_needs_a_slot_or_a_source(self):
        self._effect_list([(0x05, 0.1)])
        self.assertEqual(self.s.read()[("effect", "thorns")], 0)
        no_page = mock.patch.object(memscan, "alloc", return_value=None)
        no_page.start()
        self.addCleanup(no_page.stop)
        with self.assertRaisesRegex(session.SessionError, "没有空位.*改不了"):
            self.s.write("effect", "thorns", 0.5)
        with self.assertRaises(session.SessionError):
            self.s.pin("effect", "thorns", 0.5)                   # and no lock is left behind
        self.assertNotIn(("effect", "thorns"), self.s.pinned())

    def test_thorns_lock_survives_the_game_rebuilding_the_list(self):
        heap = self._effect_list([(0x16, 0.3)])
        self.s.pin("effect", "thorns", 0.9)
        self.s.tick()
        self.mem.put(heap + 4, struct.pack("<f", 0.3))           # gear recomputed
        self.s.tick()
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.9, places=5)

    def test_effect_lock_writes_only_when_the_sum_moves(self):
        heap = self._effect_list([(0x05, 0.1), (0x16, 0.3)], cap=4)
        self.s.pin("effect", "thorns", 0.77)
        memscan.write.reset_mock()
        for _ in range(3):
            self.s.tick()
        memscan.write.assert_not_called()                         # every 20 ms: nothing to do
        self.mem.put(heap + 12, struct.pack("<f", 0.5))           # the talent changed
        self.s.tick()
        self.assertTrue(memscan.write.called)
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.77, places=5)
        rebuilt = self.mem.add(0x98000, 0x100)                    # a new list, ours not in it
        self.mem.put(rebuilt, struct.pack("<B3xf", 0x16, 0.3))
        self.mem.put(self.obj + offsets.EFFECT_CAP, struct.pack("<QQQ", 4, rebuilt, 1))
        memscan.write.reset_mock()
        self.s.tick()
        self.assertEqual(self._list_len(), 2)                     # our entry again
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.77, places=5)
        memscan.write.reset_mock()
        self.s.tick()
        memscan.write.assert_not_called()

    def test_read_reads_each_list_once(self):
        self._effect_list([(0x16, 0.3), (0x0e, 0.01)])
        self._item_list([(0x07, 0.5), (0x12, 0.021)])
        heads = {self.obj + offsets.EFFECT_LIST[0]: 0, self.obj + offsets.ITEM_LIST[0]: 0}
        fake_read = memscan.read

        def counting(h, addr, size):
            if addr in heads:
                heads[addr] += 1
            return fake_read(h, addr, size)
        with mock.patch.object(memscan, "read", side_effect=counting):
            v = self.s.read()
        self.assertAlmostEqual(v[("effect", "thorns")], 0.3, places=5)
        self.assertAlmostEqual(v[("effect", "lifesteal")], 0.031, places=5)
        self.assertEqual(list(heads.values()), [1, 1])            # was 2 for the talent list

    def _item_list(self, items):
        """Point the hero's item Vec at `items` [(definition kind, value)]."""
        heap = self.mem.add(0xA0000, 0x2000)
        for i, (kind, value) in enumerate(items):
            definition = heap + 0x1000 + 0x80 * i
            self.mem.put(definition + offsets.ITEM_DEF_KIND, bytes([kind]))
            entry = struct.pack("<QQf4x", definition, 0, value)
            self.mem.put(heap + offsets.ITEM_STRIDE * i, entry)
        self.mem.put(self.obj + offsets.ITEM_LIST[0], struct.pack("<Q", heap))
        self.mem.put(self.obj + offsets.ITEM_LIST[1], struct.pack("<Q", len(items)))
        return heap

    def test_lifesteal_sums_effects_and_items(self):
        # RVA 0x5F809D0: effect tag 0x0e + item kind 0x12 (+ class base, not ours)
        self._effect_list([(0x16, 0.3), (0x0e, 0.01)])
        heap = self._item_list([(0x07, 0.5), (0x12, 0.021)])     # a ring among other gear
        self.assertAlmostEqual(self.s.read()[("effect", "lifesteal")], 0.031, places=5)
        self.s.write("effect", "lifesteal", 0.5)
        self.assertAlmostEqual(self.s.read()[("effect", "lifesteal")], 0.5, places=5)
        other_item = struct.unpack("<f", self.mem._read(None, heap + offsets.ITEM_VALUE, 4))[0]
        self.assertAlmostEqual(other_item, 0.5, places=5)               # kind 0x07 untouched
        self.assertAlmostEqual(self.s.read()[("effect", "thorns")], 0.3, places=5)

    def test_lifesteal_from_a_ring_alone(self):
        self._effect_list([(0x16, 0.3)])
        self._item_list([(0x12, 0.021)])
        self.assertAlmostEqual(self.s.read()[("effect", "lifesteal")], 0.021, places=5)
        self.s.write("effect", "lifesteal", 0.25)
        self.assertAlmostEqual(self.s.read()[("effect", "lifesteal")], 0.25, places=5)

    def test_lives_and_dot_stacks(self):
        # v1118 stats-line code: lives at block +0x60, stacks shown = min([+0x48], 3) + 1
        self.s.write("stat", "lives", 3)
        self.s.write("stat", "dots", session.parse("dots", "4"))  # 4 stacks typed
        v = self.s.read()
        self.assertEqual(v[("stat", "lives")], 3)
        self.assertEqual(v[("stat", "dots")], 3)                  # stored as extra stacks
        self.assertEqual(session.show("dots", v[("stat", "dots")]), "4")
        self.assertEqual(session.parse("dots", "9"), 3)           # the game's cap
        self.assertEqual(session.show("dots", 0), "1")

    def test_does_not_switch_while_ours_moves(self):
        twin = self.mem.add(0x60000, 0x2000)
        self.mem.put(twin, player_object(gold=120, chest=31))
        self.s.alternates = [twin]
        now = [100.0]
        with mock.patch.object(session.time, "monotonic", side_effect=lambda: now[0]):
            self.s.read()
            self.mem.put(self.obj, player_object(gold=130, chest=31))     # ours moved
            self.mem.put(twin, player_object(gold=125, chest=31))         # so did the twin
            now[0] += session.FOLLOW_EVERY + 0.1
            self.s.read()
        self.assertEqual(self.s.base, self.obj)

    def test_input_checks(self):
        with self.assertRaises(ValueError):
            self.s.write("player", "gold", "1.5")               # i32 field
        with self.assertRaises(ValueError):
            self.s.pin("stat", "luck", "nan")
        with self.assertRaises(ValueError):
            self.s.pin("player", "level", 30)                   # not lockable
        with mock.patch.object(session, "PLAYER_UNVERIFIED", {"banishes"}),                 self.assertRaises(ValueError):
            self.s.write("player", "banishes", 9)
        self.s.pin("stat", "luck", 5)
        self.s.pin("stat", "luck", None)
        self.assertEqual(self.s.pinned(), set())

    def test_locate_failure_is_an_error_not_an_exit(self):
        s = session.Session()
        with mock.patch.object(game, "find_pid", return_value=42),                 mock.patch.object(memscan, "open_process", return_value=7),                 mock.patch.object(memscan.k32, "CloseHandle") as close,                 mock.patch.object(player, "locate_ranked", side_effect=SystemExit("player object not found")):
            with self.assertRaisesRegex(session.SessionError, "not found"):
                s.locate()
        close.assert_called_once_with(7)
        self.assertFalse(s.connected)


PLAIN = "\n".join([
    "pick|2|1|0|elwynn|Timothy|2,10,11,2,0|2", "perk|banish:3,rerolls:3", "pets|doomguard",
    "resets|2", "hero|2|1|0|2,10,11,2,0", "marks|2|1440", "marks|5|30"])
KEY = bytes(range(1, 43)) + bytes(6)        # 42-byte key, zero-padded like the exe's block
NONCE = bytes(range(100, 116))


class SaveCodecTest(unittest.TestCase):
    def test_round_trip_and_tag(self):
        text = save.encode(KEY, NONCE, PLAIN)
        self.assertTrue(text.startswith("RSE1:"))
        self.assertEqual(save.decode(KEY, text), (NONCE, PLAIN))
        # the documented construction, spelled out independently
        raw = save.base64.b64decode(text[5:])
        ct = raw[16:-20]
        self.assertEqual(raw[-20:], hmac.new(KEY, b"tag" + NONCE + ct, hashlib.sha1).digest())
        ks0 = hmac.new(KEY, b"ks" + NONCE + (0).to_bytes(8, "little"), hashlib.sha1).digest()
        self.assertEqual(bytes(c ^ k for c, k in zip(ct[:20], ks0)), PLAIN.encode()[:20])

    def test_wrong_key_or_damage_is_refused(self):
        text = save.encode(KEY, NONCE, PLAIN)
        with self.assertRaises(save.SaveError):
            save.decode(bytes(48), text)
        raw = bytearray(save.base64.b64decode(text[5:]))
        raw[30] ^= 1
        with self.assertRaises(save.SaveError):
            save.decode(KEY, "RSE1:" + save.base64.b64encode(bytes(raw)).decode())
        with self.assertRaises(save.SaveError):
            save.decode(KEY, "not a save")

    def test_key_found_from_ipad_opad_blocks(self):
        noise = bytes((i * 37 + 11) % 251 for i in range(4096))
        blob = noise[:0x830] + bytes(b ^ 0x36 for b in KEY) + bytes(b ^ 0x5C for b in KEY) + noise
        self.assertEqual(save.key_candidates(blob), [KEY])
        self.assertEqual(save.key_candidates(noise), [])


class SaveEditTest(unittest.TestCase):
    def test_resets(self):
        lines = save.lines_of(PLAIN)
        self.assertNotIn("resets|2", save.set_resets(lines, 0))           # 0: line dropped
        self.assertIn("resets|1", save.set_resets(lines, 1))
        dropped = save.set_resets(lines, 0)
        again = save.set_resets(dropped, 2)
        self.assertEqual(again.index("resets|2"), again.index("pets|doomguard") + 1)
        with self.assertRaises(save.SaveError):
            save.set_resets(lines, -1)

    def test_marks_are_stored_in_tenths_per_class(self):
        lines = save.lines_of(PLAIN)
        self.assertEqual(save.current_class(lines), 2)
        self.assertEqual(save.marks_of(lines, 2), 144.0)
        out = save.set_marks(lines, 2, 9999.9)
        self.assertIn("marks|2|99999", out)
        self.assertIn("marks|5|30", out)                                    # other class kept
        self.assertEqual(save.changes(PLAIN, "\n".join(out)), ["-marks|2|1440", "+marks|2|99999"])
        self.assertIn("marks|8|50", save.set_marks(lines, 8, 5))            # new class: added
        with self.assertRaises(save.SaveError):
            save.set_marks(lines, 2, -1)

    def test_write_checked_backs_up_and_verifies(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        path = os.path.join(folder, "characters.txt")
        with open(path, "w", newline="") as f:
            f.write(save.encode(KEY, NONCE, PLAIN))
        new = "\n".join(save.set_marks(save.lines_of(PLAIN), 2, 5))
        backup = save.write_checked(KEY, path, NONCE, PLAIN, new)
        self.assertEqual(save.decode(KEY, save.read_text(path))[1], new)
        self.assertEqual(save.decode(KEY, save.read_text(backup))[1], PLAIN)

    def test_readers(self):
        lines = save.lines_of(PLAIN)
        self.assertEqual(save.resets_used(lines), 2)
        self.assertEqual(save.resets_used(save.set_resets(lines, 0)), 0)
        self.assertEqual(save.all_marks(lines), {2: 144.0, 5: 3.0})

    def _save_file(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        path = os.path.join(folder, "characters.txt")
        with open(path, "w", newline="") as f:
            f.write(save.encode(KEY, NONCE, PLAIN))
        with mock.patch.object(save, "find_key", return_value=KEY):
            return save.Save(path, exe="unused"), path

    def test_save_commit(self):
        sv, path = self._save_file()
        with mock.patch.object(game, "find_pid", return_value=None):
            self.assertIsNone(sv.commit(sv.lines))                         # no change: no write
            backup = sv.commit(save.set_resets(sv.lines, 0))
        self.assertTrue(os.path.exists(backup))
        self.assertEqual(save.resets_used(save.lines_of(save.decode(KEY, save.read_text(path))[1])), 0)

    def test_backups_never_overwrite_each_other(self):
        sv, path = self._save_file()
        original = save.read_text(path)
        with mock.patch.object(game, "find_pid", return_value=None):
            first = sv.commit(save.set_marks(sv.lines, 2, 1))
            sv2 = mock.patch.object(save, "find_key", return_value=KEY)
            with sv2:
                second = save.Save(path, exe="unused").commit(save.set_resets(sv.lines, 0))
        self.assertNotEqual(first, second)                                   # same second, two files
        self.assertEqual(save.read_text(first), original)                    # the original survives

    def test_save_commit_refuses_stale_read_or_running_game(self):
        sv, path = self._save_file()
        with mock.patch.object(game, "find_pid", return_value=1234):
            with self.assertRaisesRegex(save.SaveError, "running"):
                sv.commit(save.set_resets(sv.lines, 0))
        with open(path, "w", newline="") as f:                              # the game saved meanwhile
            f.write(save.encode(KEY, NONCE, PLAIN + "\nstat|kills=1"))
        with mock.patch.object(game, "find_pid", return_value=None):
            with self.assertRaisesRegex(save.SaveError, "reload"):
                sv.commit(save.set_resets(sv.lines, 0))

    def test_refuses_to_write_while_the_game_runs(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        path = os.path.join(folder, "characters.txt")
        original = save.encode(KEY, NONCE, PLAIN)
        with open(path, "w", newline="") as f:
            f.write(original)
        with mock.patch.object(sys, "argv", ["save.py", "--file", path, "--resets", "0"]),                 mock.patch.object(save, "find_key", return_value=KEY),                 mock.patch.object(game, "find_pid", return_value=1234),                 contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                save.main()
        self.assertEqual(save.read_text(path), original)
        self.assertFalse(os.path.exists(os.path.join(folder, save.BACKUP_DIR)))


if __name__ == "__main__":
    unittest.main()
