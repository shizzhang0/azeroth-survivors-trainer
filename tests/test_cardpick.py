"""Offline tests for auto card picking: rules, decisions, following the game's
output, and when keys get pressed. No game needed."""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import cardpick          # noqa: E402
from cardpick import Rules, decide, parse_text   # noqa: E402

# Two hands as v1127 printed them (trimmed to the fields we use).
LEVELUP = ('rogue: {"type":"choices","offer":3,"why":"levelup","level":6,"rerolls":2,'
           '"rerollCost":0,"gold":41,"banishes":0,"skip":false,"skipGold":10,"choices":['
           '{"rank":2,"title":"奉献 等级2","name":"奉献","rarity":0,"rarityName":"普通","kind":"upgrade"},'
           '{"title":"新法术：圣盾术","name":"圣盾术","rarity":3,"rarityName":"史诗","kind":"new"},'
           '{"title":"新法术：神圣之盾","name":"神圣之盾","rarity":1,"rarityName":"优秀","kind":"new"}]}')
CACHE = ('rogue: {"type":"choices","offer":7,"why":"cache","level":6,"rerolls":2,"rerollCost":0,'
         '"gold":21,"banishes":0,"skip":false,"skipGold":10,"choices":['
         '{"worn":0,"title":"极效灵魂石","name":"极效灵魂石","rarity":0,"rarityName":"普通","kind":"trinket"},'
         '{"worn":0,"title":"天选者印记","name":"天选者印记","rarity":3,"rarityName":"史诗","kind":"trinket"},'
         '{"worn":0,"title":"迅击戒指","name":"迅击戒指","rarity":2,"rarityName":"精良","kind":"ring"}]}')


def hand(line):
    return cardpick.parse_line(line)


def cards(*specs, why="levelup", rerolls=1, cost=0):
    return {"type": "choices", "why": why, "rerolls": rerolls, "rerollCost": cost,
            "choices": [{"name": n, "rarity": r} for n, r in specs]}


class RulesTest(unittest.TestCase):
    def test_reads_lists_and_fallback(self):
        r = parse_text("""
            # comment
            技能卡 = 神圣愤怒 > 传说 >审判
            装备卡：天选者印记，史诗、迅击戒指
            都没有时 = 重抽 > 稀有度最高   # comment
        """)
        self.assertEqual(r.skill, ["神圣愤怒", "传说", "审判"])
        self.assertEqual(r.gear, ["天选者印记", "史诗", "迅击戒指"])
        self.assertEqual(r.fallback, ("重抽", "稀有度最高"))
        self.assertTrue(r.active)

    def test_nothing_written_is_off_and_stops(self):
        for text in ("", "# 技能卡 = 审判", "技能卡 =\n装备卡 = "):
            r = parse_text(text)
            self.assertFalse(r.active)
            self.assertEqual(r.fallback, ("停下",))

    def test_bad_lines_name_their_line(self):
        for text, msg in [("都没有时 = 随便", "第 1 行.*随便"),
                          ("\n伤害 = 100", "第 2 行.*没有叫“伤害”"),
                          ("技能卡 审判", "第 1 行.*技能卡 = 名单"),
                          ("技能卡 = 审判\n\n技能卡 = 奉献", "第 3 行.*第 1 行已经写过")]:
            with self.assertRaisesRegex(cardpick.RuleError, msg):
                parse_text(text)

    def test_template_is_off_until_uncommented_and_valid_after(self):
        t = cardpick.template()
        self.assertFalse(parse_text(t).active)
        on = "\n".join(line[2:] if any(line.startswith("# " + k) for k in cardpick.KEYS)
                       else line for line in t.splitlines())
        r = parse_text(on)
        self.assertTrue(r.skill and r.gear)
        self.assertEqual(r.fallback, ("停下",))

    def test_template_written_once(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, cardpick.FILENAME)
            self.assertTrue(cardpick.ensure_file(path))
            with open(path, "w", encoding="utf-8") as f:
                f.write("技能卡 = 审判\n")
            self.assertFalse(cardpick.ensure_file(path))       # never overwrites a list
            self.assertEqual(cardpick.load(path).skill, ["审判"])
        finally:
            shutil.rmtree(d)


class DecideTest(unittest.TestCase):
    def test_first_listed_name_wins_wherever_it_sits(self):
        r = Rules(skill=["圣盾术", "奉献"])
        self.assertEqual(decide(hand(LEVELUP), r)[:2], ("pick", 1))
        self.assertEqual(decide(hand(LEVELUP), Rules(skill=["奉献", "圣盾术"]))[:2], ("pick", 0))

    def test_rarity_word_means_that_rarity_or_better(self):
        h = cards(("a", 0), ("b", 2), ("c", 1))
        self.assertEqual(decide(h, Rules(skill=["优秀"]))[:2], ("pick", 1))   # rarest match
        self.assertEqual(decide(h, Rules(skill=["传说", "精良"]))[:2], ("pick", 1))
        self.assertEqual(decide(h, Rules(skill=["传说"]))[0], None)

    def test_same_entry_twice_takes_the_rarer_then_the_left(self):
        self.assertEqual(decide(cards(("x", 1), ("x", 3)), Rules(skill=["x"]))[1], 1)
        self.assertEqual(decide(cards(("x", 2), ("x", 2)), Rules(skill=["x"]))[1], 0)

    def test_level_ups_use_skill_list_and_treasure_the_gear_list(self):
        r = Rules(skill=["圣盾术"], gear=["迅击戒指"])
        self.assertEqual(decide(hand(CACHE), r)[:2], ("pick", 2))
        self.assertEqual(decide(hand(CACHE), Rules(skill=["天选者印记"]))[0], None)  # no gear list

    def test_fallback_chain(self):
        miss = Rules(skill=["无"], fallback=("重抽", "稀有度最高"))
        self.assertEqual(decide(cards(("a", 0), ("b", 2), rerolls=1), miss)[0], "reroll")
        # out of free rerolls (or they cost gold): next fallback
        self.assertEqual(decide(cards(("a", 0), ("b", 2), rerolls=0), miss)[:2], ("pick", 1))
        self.assertEqual(decide(cards(("a", 0), ("b", 2), rerolls=3, cost=50), miss)[:2],
                         ("pick", 1))
        self.assertEqual(decide(cards(("a", 0), ("b", 2)), Rules(skill=["无"]))[0], None)
        first = Rules(skill=["无"], fallback=("第一张",))
        self.assertEqual(decide(cards(("a", 0), ("b", 2)), first)[:2], ("pick", 0))
        stop = Rules(skill=["无"], fallback=("停下", "第一张"))      # 停下 ends the chain
        self.assertEqual(decide(cards(("a", 0)), stop)[0], None)


class LogTailTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "out.txt")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, data, mode="ab"):
        with open(self.path, mode) as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)

    def test_whole_lines_only_and_restart_on_truncate(self):
        t = cardpick.LogTail(self.path)
        self.assertEqual(t.read(), ([], False))                 # no file yet
        self.write("一\r\n二")
        self.assertEqual(t.read(), (["一"], False))
        self.write("二\n")
        self.assertEqual(t.read(), (["二二"], False))
        self.write("新\n", mode="wb")                           # the game relaunched
        self.assertEqual(t.read(), (["新"], True))

    def test_split_inside_a_utf8_character(self):
        t = cardpick.LogTail(self.path)
        raw = "圣盾术\n".encode("utf-8")
        self.write(raw[:4])
        self.assertEqual(t.read()[0], [])
        self.write(raw[4:])
        self.assertEqual(t.read()[0], ["圣盾术"])


class FakeKeys:
    def __init__(self, fg=100):
        self.fg = fg
        self.taps = []

    def foreground_pid(self):
        return self.fg

    def tap(self, scan):
        self.taps.append(scan)


class PickerTest(unittest.TestCase):
    PID = 100

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = os.path.join(self.dir, "AZS-game-out.txt")
        self.rules = os.path.join(self.dir, "AZS-preset.txt")
        self.now = 0.0
        self.said = []
        self.keys = FakeKeys(self.PID)
        self.p = cardpick.CardPicker(self.log, self.rules, self.said.append,
                                     keyboard=self.keys, clock=lambda: self.now)
        self.set_rules("技能卡 = 圣盾术\n装备卡 = 史诗\n")
        open(self.log, "w").close()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def set_rules(self, text):
        with open(self.rules, "w", encoding="utf-8") as f:
            f.write(text)

    def emit(self, line):
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def deal(self, line):
        """The game prints a hand and the picker notices it on its next poll."""
        self.emit(line)
        self.step()

    def step(self, dt=0.0, pid=PID):
        self.now += dt
        self.p.poll(pid)

    def test_picks_after_the_deal_settles(self):
        self.emit(LEVELUP)
        self.step()
        self.assertEqual(self.keys.taps, [])                    # deal animation still on
        self.step(cardpick.SETTLE)
        self.assertEqual(self.keys.taps, [cardpick.SCAN_PICK[1]])
        self.step(0.1)
        self.assertEqual(len(self.keys.taps), 1)                # once, not every poll
        self.emit('rogue: {"type":"resume"}')
        self.step(cardpick.RETRY)
        self.assertEqual(len(self.keys.taps), 1)

    def test_each_new_hand_is_answered(self):
        self.deal(LEVELUP)
        self.step(cardpick.SETTLE)
        self.deal(CACHE)                                        # next stored level-up / chest
        self.step(cardpick.SETTLE)
        self.assertEqual(self.keys.taps, [cardpick.SCAN_PICK[1], cardpick.SCAN_PICK[1]])

    def test_presses_again_then_gives_up(self):
        self.emit(LEVELUP)
        for _ in range(cardpick.TRIES + 2):
            self.step(cardpick.RETRY)
        self.assertEqual(len(self.keys.taps), cardpick.TRIES)
        self.assertTrue(any("leaving it to you" in s for s in self.said))

    def test_waits_while_another_window_is_in_front(self):
        self.emit(LEVELUP)
        self.keys.fg = 555
        self.step(cardpick.SETTLE)
        self.step(1.0)
        self.assertEqual(self.keys.taps, [])
        self.keys.fg = self.PID
        self.step(1.0)
        self.assertEqual(self.keys.taps, [cardpick.SCAN_PICK[1]])

    def test_no_game_no_keys(self):
        self.deal(LEVELUP)
        self.step(cardpick.SETTLE, pid=None)
        self.assertEqual(self.keys.taps, [])

    def test_reroll(self):
        self.set_rules("技能卡 = 审判\n都没有时 = 重抽")
        self.deal(LEVELUP)
        self.step(cardpick.SETTLE)
        self.assertEqual(self.keys.taps, [cardpick.SCAN_REROLL])

    def test_lists_but_leaves_hands_without_rules(self):
        self.set_rules("# 技能卡 = 圣盾术\n")
        self.deal(LEVELUP)
        self.step(cardpick.SETTLE)
        self.assertEqual(self.keys.taps, [])
        self.assertTrue(any("圣盾术" in s for s in self.said))

    def test_rules_are_read_at_every_hand(self):
        self.set_rules("技能卡 = 奉献")
        self.deal(LEVELUP)
        self.step(cardpick.SETTLE)
        self.set_rules("技能卡 = 神圣之盾")
        self.deal(LEVELUP)
        self.step(cardpick.SETTLE)
        self.assertEqual(self.keys.taps, [cardpick.SCAN_PICK[0], cardpick.SCAN_PICK[2]])

    def test_bad_rules_pick_nothing(self):
        self.set_rules("技能卡 = 圣盾术\n都没有时 = 随便")
        self.deal(LEVELUP)
        self.step(cardpick.SETTLE)
        self.assertEqual(self.keys.taps, [])
        self.assertTrue(any("随便" in s for s in self.said))

    def test_closed_screen_or_new_launch_is_not_answered(self):
        self.emit(LEVELUP)
        self.emit('rogue: {"type":"resume"}')                   # picked by hand meanwhile
        self.step(cardpick.SETTLE)
        self.emit(LEVELUP)
        with open(self.log, "w", encoding="utf-8") as f:        # game relaunched
            f.write('rogue: {"type":"roster"}\n')
        self.step(cardpick.SETTLE)
        self.assertEqual(self.keys.taps, [])

    def test_hand_that_moves_on_right_before_the_key_is_left_alone(self):
        self.emit(LEVELUP)
        self.step()
        real = self.keys.foreground_pid

        def fg():                       # the player picks while we check the window
            self.emit('rogue: {"type":"resume"}')
            return real()
        self.keys.foreground_pid = fg
        self.step(cardpick.SETTLE)
        self.assertEqual(self.keys.taps, [])

    def test_other_lines_are_ignored(self):
        for line in ("executor: PostUpdate", 'rogue: {"type":"hzt","t":57.1}', "rogue: {oops",
                     json.dumps({"type": "choices"})):
            self.emit(line)
        self.step(cardpick.SETTLE)
        self.assertEqual(self.keys.taps, [])


class ProgramTest(unittest.TestCase):
    """The loop in cardpick.main: follows the game's life, picks in between."""

    def make(self, pids):
        self.now = 0.0
        self.pids = list(pids)          # what find_pid returns, look by look
        self.live = set()
        self.said = []

        def find_pid():
            pid = self.pids.pop(0) if self.pids else None
            if pid:
                self.live.add(pid)
            return pid

        watch = cardpick.GameWatch(find_pid, lambda pid: pid, lambda h: h in self.live,
                                   clock=lambda: self.now)
        polled = []

        class Picker:
            def poll(_self, pid):
                polled.append(pid)
                if sum(1 for x in polled if x) == 3:
                    self.live.clear()       # the game closes a few polls after it showed up

        def sleep(dt):
            self.now += dt
            if self.now > 60:
                raise TimeoutError("run() never returned")
        return watch, Picker(), polled, sleep

    def test_exit_with_game_waits_for_the_game_then_leaves_with_it(self):
        watch, picker, polled, sleep = self.make([None, 4321])
        cardpick.run(picker, watch, self.said.append, exit_with_game=True, sleep=sleep)
        self.assertIn(4321, polled)
        self.assertEqual(polled[0], None)                       # waited, didn't quit early
        self.assertTrue(any("exiting with it" in s for s in self.said))

    def test_without_exit_with_game_it_keeps_waiting(self):
        watch, picker, polled, sleep = self.make([4321])
        with self.assertRaises(TimeoutError):
            cardpick.run(picker, watch, self.said.append, exit_with_game=False, sleep=sleep)
        self.assertTrue(any("waiting for it to start again" in s for s in self.said))

    def test_a_crashing_poll_does_not_end_the_program(self):
        watch, _picker, _polled, sleep = self.make([None])

        class Bad:
            def poll(self, pid):
                raise KeyError("boom")
        with self.assertRaises(TimeoutError):
            cardpick.run(Bad(), watch, self.said.append, sleep=sleep)
        self.assertTrue(any("KeyError" in s for s in self.said))


if __name__ == "__main__":
    unittest.main()
