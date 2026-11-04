"""Offline tests: no game needed. A fake process memory stands in for the real one.

  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import struct
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import game            # noqa: E402
import memscan         # noqa: E402
import offsets         # noqa: E402
import player          # noqa: E402
import trainer         # noqa: E402
import verify_layout   # noqa: E402

SMALL_CHUNK = 0x1000   # tiny chunks so structs straddle chunk boundaries


class FakeMemory:
    """{base: bytearray} regions, patched in for memscan.regions / memscan.read."""

    def __init__(self):
        self.regions = {}

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
                                   CHUNK=SMALL_CHUNK)


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
                  chest=31, **stats):
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
        for bad in ("x36=1", "nope=1", "dmg", "dmg=abc"):
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


if __name__ == "__main__":
    unittest.main()
