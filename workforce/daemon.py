"""The daemon — the single scheduler service.

THE DAEMON IS THE SCHEDULER (ratified on oc-2, 2026-07-13): exactly one OS
service on the machine, firing every daemon-owned worker internally from
roster data. Nothing is ever installed per worker — a cadence change, a new
worker, a paused lane are all roster edits, picked up on the next tick.

Ownership rule: a worker is daemon-owned iff its ``schedule`` parses as a
five-field cron expression (schedule.maybe_cron). Informational strings
("manual", "launchd ... (legacy)") are never fired — that's how migration
stays one-lane-at-a-time: flipping a lane IS the roster edit.

Mechanics:
  - Tick on the minute boundary; a worker fires when its cron matches the
    tick's UTC minute. Missed minutes (host asleep) are not replayed —
    conservative skips are correct, the next matching minute picks up.
  - Each fire runs engine.dispatch in its own thread; the engine's §3
    per-worker lock already guarantees single-flight, so an overrunning
    shift makes the next fire a clean SKIP, never an overlap.
  - Single instance: a pidfile-checked heartbeat (local/daemon.json),
    rewritten every tick — this is the machine-readable seam RUNNER_SPEC
    §8 says the board reads (last tick, next fire per worker, pid).
"""

import datetime
import json
import os
import signal
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import engine, roster as roster_mod
from .schedule import maybe_cron

HEARTBEAT = "daemon.json"
STALE_TICK_SECS = 180  # heartbeat older than this = daemon presumed dead


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: Optional[datetime.datetime]) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, TypeError):
        return False
    return True


def read_heartbeat(local_root: str) -> Optional[dict]:
    try:
        with open(os.path.join(local_root, HEARTBEAT), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def heartbeat_status(local_root: str) -> str:
    """'running' | 'stale' | 'stopped' — the board's one-word daemon health."""
    hb = read_heartbeat(local_root)
    if not hb or not pid_alive(hb.get("pid", 0)):
        return "stopped"
    try:
        last = datetime.datetime.strptime(
            hb["last_tick"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (KeyError, ValueError):
        return "stale"
    return "running" if (_utcnow() - last).total_seconds() < STALE_TICK_SECS else "stale"


class Daemon:
    def __init__(self, base: str, local_root: str) -> None:
        self.base = base
        self.local_root = local_root
        self.started_at = _utcnow()
        # worker -> last fired minute key, restored from the prior heartbeat so
        # a restart inside an already-fired minute doesn't re-fire it (oc-9:
        # observed 04:20:38 re-fire after bootstrap). Keys are absolute minute
        # timestamps, so a stale entry only guards the exact interrupted minute
        # — a later minute never matches, so no staleness window is needed.
        self._fired: Dict[str, str] = self._reload_fired()
        self._threads: Dict[str, threading.Thread] = {}
        self._draining = False
        self._wake = threading.Event()  # set by begin_drain to cut the sleep short

    def _reload_fired(self) -> Dict[str, str]:
        """Last-fired minute keys from the prior heartbeat, or {} on a fresh
        install. The double-start guard in run() still refuses to co-run with a
        live daemon, so inheriting a dead predecessor's cursor is safe."""
        hb = read_heartbeat(self.local_root)
        fired = hb.get("fired") if hb else None
        return dict(fired) if isinstance(fired, dict) else {}

    def _log(self, msg: str) -> None:
        print("%s daemon %s" % (_iso(_utcnow()), msg), flush=True)

    def _roster(self) -> Optional[roster_mod.Roster]:
        try:
            return roster_mod.load(base=self.base)   # reload every tick: roster is data
        except roster_mod.RosterError as exc:
            self._log("ERROR roster unreadable: %s" % exc)
            return None

    def begin_drain(self, signum: Optional[int] = None) -> None:
        """Stop firing, let in-flight shifts finish, then exit — SIGTERM's
        meaning.
        launchctl bootout sends SIGTERM first; the plist's ExitTimeOut gives
        this drain room to finish inside the largest shift budget."""
        if not self._draining:
            self._draining = True
            self._wake.set()
            self._log("drain begins (signal %s) — no new fires; waiting on in-flight shifts"
                      % (signum if signum is not None else "-"))

    def in_flight(self) -> List[str]:
        return sorted(n for n, t in self._threads.items() if t.is_alive())

    def _write_heartbeat(self, now: datetime.datetime, roster: Optional[roster_mod.Roster]) -> None:
        workers = {}
        if roster:
            for name in sorted(roster.workers):
                cron = maybe_cron(roster.workers[name].schedule)
                workers[name] = {
                    "schedule": roster.workers[name].schedule,
                    "owned": bool(cron),
                    "next_fire": _iso(cron.next_fire(now)) if cron else "",
                }
        payload = {
            "pid": os.getpid(),
            "started_at": _iso(self.started_at),
            "last_tick": _iso(now),
            "state": "draining" if self._draining else "scheduling",
            "in_flight": self.in_flight(),
            "fired": dict(self._fired),   # last-fired minute per worker; reloaded on restart
            "workers": workers,
        }
        path = os.path.join(self.local_root, HEARTBEAT)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)

    def _fire(self, worker: roster_mod.Worker) -> None:
        rc = engine.dispatch(worker, self.local_root)
        self._log("fired %s rc=%d" % (worker.name, rc))

    def tick(self, now: Optional[datetime.datetime] = None, wait: bool = False) -> int:
        """One scheduler pass; returns the number of workers fired."""
        now = now or _utcnow()
        minute_key = now.strftime("%Y-%m-%dT%H:%M")
        roster = self._roster()
        fired = 0
        if roster and not self._draining:
            for name in sorted(roster.workers):
                w = roster.workers[name]
                cron = maybe_cron(w.schedule)
                if not cron or not cron.matches(now):
                    continue
                if self._fired.get(name) == minute_key:
                    continue  # already fired this minute (late/duplicate tick)
                prev = self._threads.get(name)
                if prev and prev.is_alive():
                    # engine lock would SKIP anyway; don't stack threads on it
                    self._log("skip %s: previous shift thread still running" % name)
                    self._fired[name] = minute_key
                    continue
                self._fired[name] = minute_key
                t = threading.Thread(target=self._fire, args=(w,), daemon=False,
                                     name="shift-%s" % name)
                self._threads[name] = t
                t.start()
                fired += 1
                if wait:
                    t.join()
        self._write_heartbeat(now, roster)
        return fired

    def fire_now(self, name: str) -> Tuple[bool, str]:
        """Manual trigger — the third trigger source (time/event/manual).

        Same engine path as a scheduled fire; the §3 lock still guarantees
        single-flight, so a manual fire during a shift is a clean SKIP.
        """
        roster = self._roster()
        if not roster:
            return False, "roster unreadable"
        if name not in roster.workers:
            return False, "no such worker"
        prev = self._threads.get(name)
        if prev and prev.is_alive():
            return False, "shift already in flight"
        t = threading.Thread(target=self._fire, args=(roster.workers[name],),
                             daemon=False, name="manual-%s" % name)
        self._threads[name] = t
        t.start()
        self._log("manual dispatch %s" % name)
        return True, "dispatched"

    def start_board(self) -> Optional[threading.Thread]:
        """Serve the board from the daemon process — the ONE service carries
        its own UI across reboots. Port held elsewhere (e.g. a standalone
        `workforce board`) is not fatal: scheduling never depends on it."""
        from . import board
        try:
            httpd = board.make_server(local_root=self.local_root, daemon=self)
        except OSError as exc:
            self._log("WARN board not served (port busy?): %s" % exc)
            return None
        t = threading.Thread(target=httpd.serve_forever, daemon=True, name="board")
        t.start()
        self._log("board serving on http://127.0.0.1:%d" % httpd.server_address[1])
        return t

    def run(self, with_board: bool = True) -> int:
        hb = read_heartbeat(self.local_root)
        if hb and hb.get("pid") != os.getpid() and pid_alive(hb.get("pid", 0)):
            print("daemon already running (pid %s) — refusing to double-start"
                  % hb["pid"], file=sys.stderr)
            return 1
        self._log("start pid=%d base=%s" % (os.getpid(), self.base))
        signal.signal(signal.SIGTERM, lambda s, f: self.begin_drain(s))
        signal.signal(signal.SIGINT, lambda s, f: self.begin_drain(s))
        if with_board:
            self.start_board()
        while True:
            self.tick()
            if self._draining:
                waiting = self.in_flight()
                if waiting:
                    self._log("draining: waiting on %s" % ", ".join(waiting))
                    for t in list(self._threads.values()):
                        t.join()  # each shift is bounded by its own budget
                self._write_heartbeat(_utcnow(), self._roster())
                self._log("drained cleanly — exiting")
                return 0
            now = time.time()
            # minute-boundary sleep, interruptible by begin_drain
            self._wake.wait(timeout=max(1.0, 60.0 - (now % 60.0)))


def default_service_path() -> str:
    """PATH for the service: launchd gives a bare system PATH that no user-
    installed vendor CLI lives on. Conventional user-CLI dirs, existing only —
    deliberately NOT the generating shell's PATH (session pollution). A worker
    needing more sets PATH in its roster ``env`` (the engine honors it for
    preflight and spawn alike)."""
    candidates = [
        os.path.expanduser("~/.local/bin"),
        "/usr/local/bin", "/opt/homebrew/bin",
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]
    return ":".join(p for p in candidates if os.path.isdir(p))


def plist_xml(base: str, python: Optional[str] = None, path: Optional[str] = None) -> str:
    """The single launchd agent — the one bootstrap artifact (macOS)."""
    from xml.sax.saxutils import escape
    python = python or sys.executable
    path = path or default_service_path()
    log = os.path.join(base, "local", "daemon.log")
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.workforce.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>%s</string>
        <string>-m</string>
        <string>workforce</string>
        <string>daemon</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>%s</string>
    </dict>
    <key>WorkingDirectory</key><string>%s</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ExitTimeOut</key><integer>3900</integer>
    <key>StandardOutPath</key><string>%s</string>
    <key>StandardErrorPath</key><string>%s</string>
</dict>
</plist>
""" % (escape(python), escape(path), escape(base), escape(log), escape(log))
