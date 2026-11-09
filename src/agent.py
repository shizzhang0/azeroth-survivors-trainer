"""Background agent for the in-game controller (addon AZSRemote).

  python agent.py                 (or double-click remote.bat in the repo root)

Start it by hand, before or after the game. It waits for the game, finds the
addon's mailbox in memory, locates the hero, then carries out what you click in
the in-game panel (AZS button or /azs) and reports values back. When the game
moves the hero mid-run it finds it again by itself and keeps the locks. When
the game closes it waits for the next launch; closing this window is what
stops it (and every lock). With --exit-with-game (AZS-Launcher.bat passes it)
it quits instead, once the game it saw has closed.

It needs no install path: the addon is copied into the game by
AZS-Launcher.bat (see README). With --preset FILE it also applies that file at
the start of every run: see preset.py. With --game-log FILE (the game's
output, which AZS-Launcher.bat saves there) it also picks cards by the
技能卡 / 装备卡 lines of that preset: see cardpick.py.
"""
import argparse
import ctypes
import sys
import threading
import time

import game
import addon_mailbox as mb
import cardpick
import memscan
import preset
from session import FIELDS, Session, SessionError, show

TICK = 0.05              # mailbox poll
SNAPSHOT_EVERY = 0.25    # values written back to the panel
HEARTBEAT_EVERY = 0.5
WAIT_GAME = 2.0
RELOCATE_RETRY = 5.0  # after a failed relocate


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def handle(session, ready, cmd, arg1, arg2):
    """Carry out one panel request. Returns a mailbox.RESULT code and a log line."""
    cmd = int(cmd)
    if cmd == mb.RELOCATE:
        return mb.OK, "relocate requested"
    if not ready:
        return mb.NOT_READY, "not ready yet"
    try:
        if cmd == mb.HEAL:
            return mb.OK, f"heal -> {session.heal():.0f}"
        if cmd == mb.UNLOCK_ALL:
            for key in session.pinned():
                session.pin(*key, None)
            return mb.OK, "unlock all"
        i = int(arg1)
        if not 1 <= i <= len(FIELDS):
            return mb.BAD_INPUT, f"field #{i} out of range"
        kind, name, label = FIELDS[i - 1][:3]
        unit = FIELDS[i - 1][5]
        # values arrive raw: the panel converts what you type from the game's units
        if cmd == mb.WRITE:
            return mb.OK, f"{label} = {show(unit, session.write(kind, name, arg2))}"
        if cmd == mb.LOCK:
            v = session.pin(kind, name, arg2)     # writes it now, then keeps it there
            return mb.OK, f"lock {label} = {show(unit, v)}"
        if cmd == mb.UNLOCK:
            session.pin(kind, name, None)
            return mb.OK, f"unlock {label}"
        return mb.BAD_INPUT, f"unknown command {cmd}"
    except ValueError as e:
        return mb.BAD_INPUT, f"bad input: {e}"
    except SessionError as e:
        return mb.FAILED, f"failed: {e}"


class Agent:
    def __init__(self, session_factory=Session, preset_path=None, exit_with_game=False):
        self.session_factory = session_factory
        self.preset_path = preset_path      # None: no preset
        self.exit_with_game = exit_with_game    # quit when the game closes, not wait
        self.session = None
        self.box = None
        self.h = None
        self.pid = None
        self.last_req = None
        self.locating = False
        self.located_ok = False
        self.retry_at = 0.0
        self.incompatible = None         # base of a mismatched mailbox we already reported
        self.searching = False           # "looking for the addon" already said
        self.quiet_reported = False      # "addon not ticking" already said
        self.beat = 0

    # --- player object, located off the main loop (it takes ~30 s) ---------------
    def start_locate(self):
        if self.session:
            self.session.close()
        self.session = self.session_factory()
        self.located_ok = False
        self.locating = True
        session = self.session

        def work():
            try:
                base = session.locate()
                if session is self.session:
                    self.located_ok = True
                    log(f"hero located at 0x{base:X} — panel is live")
                    self.apply_preset(session)
            except SessionError as e:
                log(f"could not locate the hero: {e} (click 重新定位 in the panel to retry)")
            except Exception as e:      # noqa: BLE001 — keep the agent alive
                log(f"locate crashed: {type(e).__name__}: {e}")
            finally:
                if session is self.session:
                    self.locating = False

        threading.Thread(target=work, daemon=True).start()

    def apply_preset(self, session):
        """A new run's hero was found: lock what the preset file asks for. The
        file is read now, so an edit counts from the next run."""
        if not self.preset_path:
            return
        try:
            entries = preset.load(self.preset_path)
        except (OSError, preset.PresetError) as e:
            log(f"preset {self.preset_path}: {e} — nothing applied")
            return
        for line in preset.apply(session, entries):
            log(f"preset: {line}")

    def start_relocate(self):
        """Find the hero again (it moved, or 重新定位 was clicked), keeping locks."""
        session = self.session
        if not session or session.h is None:
            self.start_locate()
            return
        self.locating = True

        def work():
            try:
                old, new = session.relocate()
                if session is self.session:
                    self.located_ok = True
                    moved = "same place" if old == new else f"moved 0x{old:X} -> 0x{new:X}"
                    log(f"hero found again ({moved}); locks kept: {len(session.pinned())}")
            except SessionError as e:
                log(f"relocate failed: {e} — retrying in {RELOCATE_RETRY:.0f} s")
                self.retry_at = time.time() + RELOCATE_RETRY
            except Exception as e:      # noqa: BLE001 — keep the agent alive
                log(f"relocate crashed: {type(e).__name__}: {e}")
                self.retry_at = time.time() + RELOCATE_RETRY
            finally:
                if session is self.session:
                    self.locating = False

        threading.Thread(target=work, daemon=True).start()

    def maybe_relocate(self, now):
        """The game moved the hero mid-run: go after it without being asked."""
        s = self.session
        if (s and self.located_ok and s.lost and not self.locating and now >= self.retry_at):
            log(f"hero lost: {s.lost_reason or 'stat block gone'} — relocating, locks kept")
            self.start_relocate()

    @property
    def ready(self):
        return bool(self.session and self.located_ok and self.session.connected)

    def state(self):
        if self.locating:
            return mb.LOCATING
        return mb.READY if self.ready else mb.LOST

    # --- mailbox ------------------------------------------------------------
    def attach(self):
        """Find the mailbox of the current run (blocking sweep)."""
        if not self.searching:              # say it once per wait, not every 2 s
            log("looking for the addon in game memory (start a run if you haven't) ...")
            self.searching = True
        box = mb.find(self.h)
        if not box:
            return False
        if not box.compatible:
            # a run that started before the addon was updated: its array has
            # another layout (maybe shorter). Writing into it would overrun it.
            if self.incompatible != box.base:
                log(f"the addon in this run is a different version ({box.describe()}); "
                    "start a new run so the game loads the updated one — not touching it")
                self.incompatible = box.base
            return False
        self.box = box
        self.searching = False
        self.lua_beat, self.lua_beat_at = box.get(mb.LUA_BEAT), time.time()
        self.last_req = box.get(mb.ACK)      # never replay a request from before we came
        log(f"addon found (stride {box.stride}); locating the hero ...")
        self.start_locate()
        return True

    def lua_silent(self, now):
        """Has the addon stopped bumping LUA_BEAT? Then this array is a leftover of
        an earlier run (the UI reloaded and our addon lives elsewhere now)."""
        beat = self.box.get(mb.LUA_BEAT)
        if beat != self.lua_beat:
            self.lua_beat, self.lua_beat_at = beat, now
            self.quiet_reported = False
            return False
        return now - self.lua_beat_at > mb.LUA_SILENT

    def check_quiet_addon(self):
        """Our addon stopped ticking. Either a new run started and its addon lives
        in a new array (switch to it, fresh), or the game just isn't drawing its UI
        (in the background, paused): then keep everything, locks included."""
        other = mb.find(self.h)
        if other and other.compatible and other.base != self.box.base:
            before = other.get(mb.LUA_BEAT)
            time.sleep(0.5)
            if other.get(mb.LUA_BEAT) != before:     # only a ticking one is the new run
                log("the addon moved (new run) — switching to it")
                self.box = None                      # the loop attaches to the new one
                return
        if not self.quiet_reported:
            log("the addon is not ticking (game in the background or paused?) — "
                "keeping the connection and the locks")
            self.quiet_reported = True
        self.lua_beat_at = time.time()       # look again after another quiet spell

    def serve_once(self):
        """Handle at most one pending request, then refresh the panel's view."""
        box = self.box
        req = box.get(mb.REQ)
        if req is not None and req != self.last_req:
            cmd, a1, a2 = box.get(mb.CMD), box.get(mb.ARG1), box.get(mb.ARG2)
            if int(cmd) == mb.RELOCATE:
                self.start_relocate()
            code, line = handle(self.session, self.ready, cmd, a1, a2)
            log(f"panel: {line}")
            box.set(mb.RESULT, code)
            box.set(mb.ACK, req)
            self.last_req = req

    def snapshot(self):
        box = self.box
        box.set(mb.STATE, self.state())
        if not self.ready:
            return
        vals = self.session.read()
        while self.session.events:
            log(self.session.events.pop(0))
        if vals is None:
            return
        pinned = self.session.pinned()
        for i, (kind, name, *_rest) in enumerate(FIELDS):
            box.set(mb.VALUES + i, vals[(kind, name)])
            box.set(mb.LOCKED + i, 1 if (kind, name) in pinned else 0)

    def heartbeat(self):
        self.beat += 1
        self.box.set(mb.HEARTBEAT, self.beat)

    # --- main loop ------------------------------------------------------------
    def game_gone(self):
        """The game closed: drop everything tied to that process and wait again
        (or, with exit_with_game, let run() return)."""
        if self.exit_with_game:
            log("game closed — exiting with it")
        else:
            log("game closed — waiting for it to start again (close this window to quit)")
        self.close_handle()
        self.pid, self.box, self.located_ok, self.locating = None, None, False, False
        self.searching, self.incompatible = False, None

    def run(self):
        next_snap = next_beat = 0.0
        while True:
            # one kernel call per tick; walking every window is for finding the game
            if self.h is not None and not memscan.alive(self.h):
                self.game_gone()
                if self.exit_with_game:
                    return
            if self.h is None:
                pid = game.find_pid()
                if not pid:
                    time.sleep(WAIT_GAME)
                    continue
                self.pid = pid
                self.h = memscan.open_process(pid, write=True)
                self.box = None
                log(f"game found (pid {pid})")
            if self.box is not None and self.lua_silent(time.time()):
                self.check_quiet_addon()
            if self.box is None or not self.box.valid():
                if self.box is not None:
                    log("addon reloaded (new run?) — searching again")
                    self.box = None
                if not self.attach():
                    time.sleep(WAIT_GAME)
                    continue
            now = time.time()
            self.maybe_relocate(now)
            self.serve_once()
            if now >= next_beat:
                self.heartbeat()
                next_beat = now + HEARTBEAT_EVERY
            if now >= next_snap:
                self.snapshot()
                next_snap = now + SNAPSHOT_EVERY
            time.sleep(TICK)

    def close_handle(self):
        if self.session:
            self.session.close()
            self.session = None
        if self.h:
            memscan.k32.CloseHandle(self.h)
            self.h = None


def start_card_picker(agent, game_log, preset_path):
    """Pick cards on a thread of its own: the agent's memory sweeps take
    seconds, and a hand shouldn't wait for them."""
    try:
        rules = cardpick.load(preset_path)
    except (OSError, cardpick.RuleError) as e:
        log(f"cards: {preset_path}: {e} — fix the file; it is read again at every hand")
    else:
        if rules.active:
            log(f"cards: auto-pick on — 技能卡 {rules.skill or '（不选）'}, "
                f"装备卡 {rules.gear or '（不选）'}, 都没有时 {list(rules.fallback)}")
        else:
            log("cards: no 技能卡 / 装备卡 list in the preset — hands are only listed here")
    picker = cardpick.CardPicker(game_log, preset_path, log)
    picker.start(lambda: agent.pid)
    return picker


def single_instance():
    """Hold a named mutex; False if another agent already runs."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    single_instance.handle = k32.CreateMutexW(None, False, "Local\\AZSRemoteAgent")
    return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS


def main():
    game.utf8_console()
    ap = argparse.ArgumentParser(description="AZS in-game controller agent")
    ap.add_argument("--preset", metavar="FILE",
                    help="values to lock at the start of every run; an empty template is "
                         "written if FILE doesn't exist")
    ap.add_argument("--no-preset", action="store_true", help="don't apply any preset")
    ap.add_argument("--game-log", metavar="FILE",
                    help="the game's standard output (AZS-Launcher.bat saves it there): "
                         "pick cards by the preset's 技能卡 / 装备卡 lines")
    ap.add_argument("--exit-with-game", action="store_true",
                    help="quit once the game closes instead of waiting for the next launch "
                         "(AZS-Launcher.bat passes this)")
    args = ap.parse_args()
    if not single_instance():
        sys.exit("another agent is already running")
    path = None if args.no_preset else args.preset
    if path:
        if preset.ensure_file(path):
            log(f"wrote an empty preset to {path} — edit it to lock values every run")
        try:
            n = len(preset.load(path))
            log(f"preset {path}: {n} value(s) applied at the start of every run")
        except (OSError, preset.PresetError) as e:
            log(f"preset {path}: {e} — fix the file; it is read again at every run")
    log("agent started. In a run, click the AZS button (or /azs). Close this window to stop.")
    agent = Agent(preset_path=path, exit_with_game=args.exit_with_game)
    if args.game_log and path:
        start_card_picker(agent, args.game_log, path)
    try:
        agent.run()
    except KeyboardInterrupt:
        log("stopped")
    finally:
        agent.close_handle()


if __name__ == "__main__":
    main()
