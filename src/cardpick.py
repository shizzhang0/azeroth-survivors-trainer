"""Auto-pick level-up (spell) and treasure (ring / trinket / amulet) cards.

  cards.bat --cards AZS-cards.txt --game-log AZS-game-out.txt [--exit-with-game]

A program of its own (cards.bat in the repo root): it needs neither the
addon nor the agent, only the game's output and a file of priority lists.
AZS-Launcher.bat runs it next to remote.bat in one window.

The game prints every hand it deals on its standard output, one line each:

    rogue: {"type":"choices","offer":7,"why":"cache","rerolls":2,"rerollCost":0,
            "choices":[{"name":"天选者印记","kind":"trinket","rarity":3,...},...]}

and `rogue: {"type":"resume"}` when the card screen closes. Nothing keeps the
hand in memory in a readable form (the one copy found there was a stale
offer 1), so AZS-Launcher.bat starts the game with stdout going to a file and
this program follows that file (LogTail). Rules come from AZS-cards.txt:

    技能卡 = 神圣愤怒 > 传说 > 审判        升级时的法术卡，越靠前越优先
    装备卡 = 天选者印记 > 史诗              宝箱 / 首领给的戒指、饰品、项链
    都没有时 = 重抽 > 稀有度最高            名单里一张都没有时，依次试这些

A name matches that card; a rarity word (普通 优秀 精良 史诗 传说) matches any
card of that rarity or better. When several cards match the same entry the
rarer one wins, then the one further left. 都没有时 takes 停下 (leave it to
you, the default), 重抽 (only while rerolls are free), 稀有度最高, 第一张.

Picking presses the game's own keys (1 2 3 4, R to reroll) with SendInput, and
only while the game window is in front: keys posted to a background or
minimized window are ignored (v1127), and a key pressed into the wrong window
would land somewhere else. If the screen doesn't move on after a key, the
picker tries twice more and then leaves that hand alone.
"""
import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

SKILL, GEAR, FALLBACK = "技能卡", "装备卡", "都没有时"
KEYS = (SKILL, GEAR, FALLBACK)
RARITY = {"普通": 0, "优秀": 1, "精良": 2, "史诗": 3, "传说": 4}
STOP, REROLL, RAREST, FIRST = "停下", "重抽", "稀有度最高", "第一张"
FALLBACKS = (STOP, REROLL, RAREST, FIRST)
_SEPS = (">", "＞", ",", "，", "、", "|", "｜")

LINE_PREFIX = "rogue: "
SETTLE = 0.5            # s after a hand appears before the first key (the deal animation)
RETRY = 2.0             # s to wait for the screen to move on before pressing again
TRIES = 3
POLL = 0.05
WAIT_GAME = 2.0         # s between looks for the game window while it isn't running
FILENAME = "AZS-cards.txt"

# scan codes: 1 2 3 4 and R (the game's hint: "1 2 3 picks a card · R rerolls")
SCAN_PICK = (0x02, 0x03, 0x04, 0x05)
SCAN_REROLL = 0x13


class RuleError(ValueError):
    pass


class Rules:
    def __init__(self, skill=None, gear=None, fallback=(STOP,)):
        self.skill = skill          # [entry] or None: that kind of hand is left alone
        self.gear = gear
        self.fallback = tuple(fallback)

    @property
    def active(self):
        return bool(self.skill or self.gear)

    def __eq__(self, other):
        return (isinstance(other, Rules) and (self.skill, self.gear, self.fallback)
                == (other.skill, other.gear, other.fallback))

    def __repr__(self):
        return f"Rules(skill={self.skill}, gear={self.gear}, fallback={self.fallback})"


def _split(val):
    parts = [val]
    for sep in _SEPS:
        parts = [p for chunk in parts for p in chunk.split(sep)]
    return [p.strip() for p in parts if p.strip()]


def split_line(line):
    """(key, value) of a `key = value` line, or None for a blank / comment line."""
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    for sep in ("=", "＝", ":", "："):
        if sep in line:
            key, val = line.split(sep, 1)
            return key.strip(), val.strip()
    return line, None


def parse_text(text):
    """Rules from AZS-cards.txt's text; RuleError names the bad line."""
    found, seen = {}, {}
    for no, line in enumerate(text.splitlines(), 1):
        kv = split_line(line)
        if not kv:
            continue
        key, val = kv
        if val is None:
            raise RuleError(f"第 {no} 行：要写成“技能卡 = 名单”：{key}")
        if key not in KEYS:
            raise RuleError(f"第 {no} 行：没有叫“{key}”的设置（只认 {' / '.join(KEYS)}）")
        if key in seen:
            raise RuleError(f"第 {no} 行：{key} 在第 {seen[key]} 行已经写过了")
        seen[key] = no
        items = _split(val or "")
        if not items:
            continue                    # "技能卡 =" with nothing after it: off
        if key == FALLBACK:
            bad = [i for i in items if i not in FALLBACKS]
            if bad:
                raise RuleError(f"第 {no} 行：{FALLBACK} 只能写 {' / '.join(FALLBACKS)}，"
                                f"不认识“{bad[0]}”")
        found[key] = items
    return Rules(found.get(SKILL), found.get(GEAR), found.get(FALLBACK, (STOP,)))


def load(path):
    """Rules from the file; inactive Rules if there is no file."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return parse_text(f.read())
    except FileNotFoundError:
        return Rules()


def _matches(entry, card):
    if entry in RARITY:
        return card.get("rarity", -1) >= RARITY[entry]
    return card.get("name") == entry


def label(card):
    rank = f" 等级{card['rank']}" if card.get("rank") else ""
    return f"{card.get('name', '?')}{rank}（{card.get('rarityName', '?')}）"


def decide(hand, rules):
    """("pick", index, why) / ("reroll", None, why) / (None, None, why)."""
    cards = hand.get("choices") or []
    if not cards:
        return None, None, "空手牌"
    wanted = rules.skill if hand.get("why") == "levelup" else rules.gear
    if not wanted:
        return None, None, "没写这类卡的名单"
    for n, entry in enumerate(wanted, 1):
        hits = [i for i, c in enumerate(cards) if _matches(entry, c)]
        if hits:
            best = max(hits, key=lambda i: (cards[i].get("rarity", -1), -i))
            return "pick", best, f"名单第 {n} 项“{entry}”"
    for fb in rules.fallback:
        if fb == STOP:
            break
        if fb == REROLL:
            if hand.get("rerolls", 0) > 0 and not hand.get("rerollCost"):
                return "reroll", None, "名单里没有，重抽"
            continue
        if fb == RAREST:
            best = max(range(len(cards)), key=lambda i: (cards[i].get("rarity", -1), -i))
            return "pick", best, "名单里没有，选稀有度最高"
        if fb == FIRST:
            return "pick", 0, "名单里没有，选第一张"
    return None, None, "名单里没有，留给你选"


def parse_line(line):
    """The JSON message of a `rogue: {...}` line, or None."""
    if not line.startswith(LINE_PREFIX + "{"):
        return None
    try:
        msg = json.loads(line[len(LINE_PREFIX):])
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


class LogTail:
    """New complete lines of a file that another process appends to. The game
    rewrites it from scratch at every launch: a shrink starts over."""

    def __init__(self, path):
        self.path = path
        self.pos = 0
        self.buf = b""

    def read(self):
        """(new lines, restarted?) — restarted when the file was truncated."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return [], False
        restarted = size < self.pos
        if restarted:
            self.pos, self.buf = 0, b""
        if size == self.pos:
            return [], restarted
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(size - self.pos)
        except OSError:
            return [], restarted
        self.pos += len(data)
        chunks = (self.buf + data).split(b"\n")
        self.buf = chunks.pop()             # an unfinished last line waits for the rest
        return [c.decode("utf-8", "replace").rstrip("\r") for c in chunks], restarted


# --- keyboard --------------------------------------------------------------------
class _KI(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KI), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


class Keyboard:
    """SendInput into whatever window is in front — so check that first."""
    KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0002, 0x0008

    def __init__(self):
        self.u32 = ctypes.WinDLL("user32", use_last_error=True)
        self.u32.GetForegroundWindow.restype = wintypes.HWND
        self.u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                      ctypes.POINTER(wintypes.DWORD)]

    def foreground_pid(self):
        pid = wintypes.DWORD()
        self.u32.GetWindowThreadProcessId(self.u32.GetForegroundWindow(), ctypes.byref(pid))
        return pid.value

    def _send(self, scan, up):
        i = _INPUT(type=1)
        i.ki = _KI(0, scan, self.KEYEVENTF_SCANCODE | (self.KEYEVENTF_KEYUP if up else 0), 0, 0)
        return self.u32.SendInput(1, ctypes.byref(i), ctypes.sizeof(_INPUT))

    def tap(self, scan):
        self._send(scan, False)
        time.sleep(0.06)            # one frame or more, so the game sees it go down
        self._send(scan, True)


# --- the picker ------------------------------------------------------------------
class CardPicker:
    """Follows the game's output and answers each hand by the rules; poll(pid)
    does one step."""

    def __init__(self, log_path, rules_path, say, keyboard=None, clock=time.monotonic):
        self.tail = LogTail(log_path)
        self.log_path = log_path
        self.rules_path = rules_path
        self.say = say
        self.kb = keyboard or Keyboard()
        self.clock = clock
        self.hand = None            # the hand on screen, or None
        self.seen_at = 0.0
        self.tries = 0
        self.next_at = 0.0
        self.done = False           # this hand is decided (picked, given up, or left)
        self.plan = None            # (action, index, why) for this hand
        self.missing_said = False
        self.waiting_said = False

    def feed(self, line):
        msg = parse_line(line)
        if not msg:
            return
        t = msg.get("type")
        if t == "choices":
            self.hand, self.seen_at = msg, self.clock()
            self.tries, self.next_at, self.done, self.plan = 0, self.seen_at + SETTLE, False, None
            self.waiting_said = False
        elif t in ("resume", "start", "defeat", "victory", "logout", "roster", "end"):
            self.hand = None

    def catch_up(self):
        lines, restarted = self.tail.read()
        if restarted:
            self.hand = None
        for line in lines:
            self.feed(line)
        return lines

    def poll(self, pid):
        if not os.path.exists(self.log_path):
            if not self.missing_said:
                self.say(f"cards: {self.log_path} not found — auto-pick needs the game "
                         "started by AZS-Launcher.bat")
                self.missing_said = True
            return
        self.missing_said = False
        self.catch_up()
        if not pid or not self.hand or self.done or self.clock() < self.next_at:
            return
        if self.plan is None:
            try:
                rules = load(self.rules_path)
            except (OSError, RuleError) as e:
                self.say(f"cards: {self.rules_path}: {e} — not picking")
                self.done = True
                return
            h = self.hand
            names = " / ".join(f"{i + 1}.{label(c)}" for i, c in enumerate(h.get("choices", [])))
            self.say(f"cards: hand #{h.get('offer')} ({h.get('why')}): {names}")
            if not rules.active:
                self.done = True            # listed, so names can be copied into the list
                return
            self.plan = decide(self.hand, rules)
            if self.plan[0] is None:
                self.say(f"cards: {self.plan[2]}")
                self.done = True
                return
        if self.tries >= TRIES:
            self.say(f"cards: hand #{self.hand.get('offer')} didn't move after {TRIES} presses "
                     "— leaving it to you")
            self.done = True
            return
        if self.kb.foreground_pid() != pid:
            if not self.waiting_said:
                self.say("cards: waiting for the game window to be in front")
                self.waiting_said = True
            self.next_at = self.clock() + POLL * 4
            return
        # one last look: a hand that moved on while we waited must not get our key
        self.catch_up()
        if not self.hand or self.done:
            return
        action, index, why = self.plan
        if action == "pick":
            self.kb.tap(SCAN_PICK[index])
            what = f"picked {index + 1}. {label(self.hand['choices'][index])}"
        else:
            self.kb.tap(SCAN_REROLL)
            what = "reroll"
        self.tries += 1
        self.next_at = self.clock() + RETRY
        self.say(f"cards: {what} — {why}" + (f" (try {self.tries})" if self.tries > 1 else ""))



# --- the program -----------------------------------------------------------------
def template():
    """A file whose lists are all commented out: nothing is picked until edited."""
    return "\n".join([
        "# AZS 自动选卡名单。cards.bat（AZS-Launcher.bat 会开它）每出一手牌都按这里选。",
        "# 用法：删掉行首的 #，写上名单，保存；下一手牌就生效，不用重启。",
        "# 越靠前越优先，用 > 隔开。写名字只认那张卡（照卡上写，不带“等级2”；窗口里会列出每手牌，可以照抄）；",
        "# 写稀有度（普通 优秀 精良 史诗 传说）就是这个稀有度及以上的任意一张。同一项有几张都符合时选更稀有的，再一样选靠左的。",
        "# 只在游戏窗口在最前面时按键（1 2 3 选卡、R 重抽）；切走了就等你切回来再选。",
        "",
        "# 技能卡 = 神圣愤怒 > 传说 > 审判        # 升级给的法术卡",
        "# 装备卡 = 天选者印记 > 史诗              # 宝箱 / 守卫给的戒指、饰品、项链",
        "# 都没有时 = 停下                        # 停下（留给你选）/ 重抽（只用免费次数）/ 稀有度最高 / 第一张，可以用 > 连着写",
    ]) + "\n"


def ensure_file(path):
    """Write the commented-out template if there is no file yet. True if written."""
    if os.path.exists(path):
        return False
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(template())
        return True
    except OSError:
        return False


def describe(rules):
    if not rules.active:
        return "no 技能卡 / 装备卡 list yet — hands are only listed here"
    return (f"技能卡 {rules.skill or '（不选）'}, 装备卡 {rules.gear or '（不选）'}, "
            f"都没有时 {list(rules.fallback)}")


class GameWatch:
    """The game's pid while it runs (None otherwise)."""

    def __init__(self, find_pid, open_process, alive, clock=time.monotonic):
        self.find_pid, self.open_process, self.alive = find_pid, open_process, alive
        self.clock = clock
        self.h = self.pid = None
        self.next_look = 0.0

    def poll(self):
        if self.h is not None and not self.alive(self.h):
            self.h = self.pid = None
        if self.h is None and self.clock() >= self.next_look:
            self.next_look = self.clock() + WAIT_GAME
            pid = self.find_pid()
            if pid:
                self.h, self.pid = self.open_process(pid), pid
        return self.pid


def run(picker, watch, say, exit_with_game=False, sleep=time.sleep):
    """Pick until stopped, or until the game closes with exit_with_game."""
    was = None
    while True:
        pid = watch.poll()
        if pid != was:
            if pid:
                say(f"cards: game found (pid {pid})")
            elif was:
                if exit_with_game:
                    say("cards: game closed — exiting with it")
                    return
                say("cards: game closed — waiting for it to start again")
            was = pid
        try:
            picker.poll(pid)
        except Exception as e:      # noqa: BLE001 — one bad hand must not end the picker
            say(f"cards: {type(e).__name__}: {e}")
            sleep(1.0)
        sleep(POLL)


def single_instance():
    """Hold a named mutex; False if another card picker already runs."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    single_instance.handle = k32.CreateMutexW(None, False, "Local\\AZSCardPicker")
    return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS


def say(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def main():
    import game
    import memscan
    game.utf8_console()
    ap = argparse.ArgumentParser(description="AZS card auto-pick")
    ap.add_argument("--cards", required=True, metavar="FILE",
                    help="priority lists (技能卡 / 装备卡 / 都没有时); a commented-out template "
                         "is written if FILE doesn't exist")
    ap.add_argument("--game-log", required=True, metavar="FILE",
                    help="the game's standard output (AZS-Launcher.bat saves it there)")
    ap.add_argument("--exit-with-game", action="store_true",
                    help="quit once the game closes instead of waiting for the next launch")
    args = ap.parse_args()
    if not single_instance():
        sys.exit("another card picker is already running")
    if ensure_file(args.cards):
        say(f"cards: wrote an empty list to {args.cards} — edit it to pick cards")
    try:
        say(f"cards: {args.cards}: {describe(load(args.cards))}")
    except (OSError, RuleError) as e:
        say(f"cards: {args.cards}: {e} — fix the file; it is read again at every hand")
    say("cards: started. Keys go to the game only while its window is in front.")
    picker = CardPicker(args.game_log, args.cards, say)
    watch = GameWatch(game.find_pid, memscan.open_process, memscan.alive)
    try:
        run(picker, watch, say, args.exit_with_game)
    except KeyboardInterrupt:
        say("cards: stopped")


if __name__ == "__main__":
    main()
