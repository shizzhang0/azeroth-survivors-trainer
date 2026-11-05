"""Background agent for the in-game controller (addon AZSRemote).

  python agent.py                 (or double-click remote.bat in the repo root)

Start it by hand, before or after the game. It keeps the addon installed,
waits for the game, finds the addon's mailbox in memory, locates the hero, then
carries out what you click in the in-game panel (/azs) and reports values back.
It exits when the game closes. Closing this window stops every lock.
"""
import ctypes
import filecmp
import os
import shutil
import sys
import threading
import time

import game
import addon_mailbox as mb
import memscan
from scan_stores import DEFAULT_EXE
from session import FIELDS, Session, SessionError

TICK = 0.05              # mailbox poll
SNAPSHOT_EVERY = 0.25    # values written back to the panel
HEARTBEAT_EVERY = 0.5
WAIT_GAME = 2.0
ADDON = "AZSRemote"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON_SRC = os.path.join(REPO, "addon", ADDON)
ADDON_DST = os.path.join(os.path.dirname(DEFAULT_EXE), "benilla-config", "AddOns", ADDON)


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def install_addon(src=ADDON_SRC, dst=ADDON_DST):
    """Copy the addon into the game's AddOns folder if missing or out of date.
    Returns True when files were copied (the game picks them up next run)."""
    names = sorted(os.listdir(src))
    if os.path.isdir(dst) and all(
            os.path.exists(os.path.join(dst, n)) and
            filecmp.cmp(os.path.join(src, n), os.path.join(dst, n), shallow=False) for n in names):
        return False
    os.makedirs(dst, exist_ok=True)
    for n in names:
        shutil.copy2(os.path.join(src, n), os.path.join(dst, n))
    return True


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
        if cmd == mb.WRITE:
            return mb.OK, f"{label} = {session.write(kind, name, arg2)}"
        if cmd == mb.LOCK:
            v = session.pin(kind, name, arg2)
            session.write(kind, name, v)      # take effect now, not at the next tick
            return mb.OK, f"lock {label} = {v}"
        if cmd == mb.UNLOCK:
            session.pin(kind, name, None)
            return mb.OK, f"unlock {label}"
        return mb.BAD_INPUT, f"unknown command {cmd}"
    except ValueError as e:
        return mb.BAD_INPUT, f"bad input: {e}"
    except SessionError as e:
        return mb.FAILED, f"failed: {e}"


class Agent:
    def __init__(self, session_factory=Session):
        self.session_factory = session_factory
        self.session = None
        self.box = None
        self.h = None
        self.pid = None
        self.last_req = None
        self.locating = False
        self.located_ok = False
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
            except SessionError as e:
                log(f"could not locate the hero: {e} (click 重新定位 in the panel to retry)")
            except Exception as e:      # noqa: BLE001 — keep the agent alive
                log(f"locate crashed: {type(e).__name__}: {e}")
            finally:
                if session is self.session:
                    self.locating = False

        threading.Thread(target=work, daemon=True).start()

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
        log("looking for the addon in game memory (start a run if you haven't) ...")
        box = mb.find(self.h)
        if not box:
            return False
        self.box = box
        self.last_req = box.get(mb.ACK)      # never replay a request from before we came
        log(f"addon found (stride {box.stride}); locating the hero ...")
        self.start_locate()
        return True

    def serve_once(self):
        """Handle at most one pending request, then refresh the panel's view."""
        box = self.box
        req = box.get(mb.REQ)
        if req is not None and req != self.last_req:
            cmd, a1, a2 = box.get(mb.CMD), box.get(mb.ARG1), box.get(mb.ARG2)
            if int(cmd) == mb.RELOCATE:
                self.start_locate()
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
    def run(self):
        seen_game = False
        next_snap = next_beat = 0.0
        while True:
            pid = game.find_pid()
            if not pid:
                if seen_game:
                    log("game closed — stopping (locks end with it)")
                    return
                time.sleep(WAIT_GAME)
                continue
            if pid != self.pid:
                self.close_handle()
                self.pid, seen_game = pid, True
                self.h = memscan.open_process(pid, write=True)
                self.box = None
                log(f"game found (pid {pid})")
            if self.box is None or not self.box.valid():
                if self.box is not None:
                    log("addon reloaded (new run?) — searching again")
                    self.box = None
                if not self.attach():
                    time.sleep(WAIT_GAME)
                    continue
            now = time.time()
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


def single_instance():
    """Hold a named mutex; False if another agent already runs."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    single_instance.handle = k32.CreateMutexW(None, False, "Local\\AZSRemoteAgent")
    return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS


def main():
    if not single_instance():
        sys.exit("another agent is already running")
    if install_addon():
        log(f"installed/updated the addon in {ADDON_DST} — it loads from the next run")
    log("agent started. In a run, type /azs to open the panel. Ctrl+C or close this window to stop.")
    agent = Agent()
    try:
        agent.run()
    except KeyboardInterrupt:
        log("stopped")
    finally:
        agent.close_handle()


if __name__ == "__main__":
    main()
