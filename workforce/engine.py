"""The runner engine — RUNNER_SPEC made software, one worker at a time.

Every MUST in ProtocolCity's docs/specs/RUNNER_SPEC.md maps to a step here:

  §2 identity/signing   -> _build_env (exactly one identity; ambient store
                           scoping vars stripped from the child env)
  §3 single-flight      -> _Lock (atomic mkdir, stale reclaim, SKIP on held)
  §4 preflight          -> _preflight (CLI present, desk/queue probe, disk)
  §5 credentials        -> _fetch_secret (dispatch-time keychain read; never
                           persisted, never logged)
  §6 dispatch           -> canonical-path law reads + sha256 pins (the law-
                           version amendment), model substitution, wall-clock
                           budget with hard kill; multi-pass loop (pass
                           ceiling / budget floor / empty queue / no-progress
                           stops) when the roster sets max_passes > 1
  §7 workspace safety   -> _predirty_snapshot (pre-dispatch dirty-file list,
                           exposed to guards via env)
  §8 evidence           -> Ledger events; exit codes 0=ran/skipped, 1=infra

Exit codes: 0 = dispatched or cleanly skipped; 1 = infra failure.
Dry-run performs every step except spawning the vendor CLI.
"""

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from typing import Dict, List, Optional, Tuple, Union

from .ledger import Ledger
from .roster import Worker

# Ambient desk-scoping vars a child must never inherit (§2 — the misrouting
# incident class). The worker's own identity is set explicitly afterwards.
SCRUBBED_ENV = ("TP_AGENT_ID", "TP_PRODUCT", "TP_DEFAULT_PRODUCT")
IDENTITY_ENV = "TP_AGENT_ID"
PREDIRTY_ENV = "WORKFORCE_PREDIRTY"
LOCK_GRACE_SECS = 600


class InfraError(RuntimeError):
    """Preflight or dispatch failure that a human must notice (exit 1)."""


class _Skip(Exception):
    """Clean, recoverable non-dispatch (exit 0)."""


class _Lock:
    """Atomic per-worker mutex with stale reclaim (§3)."""

    def __init__(self, root: str, worker: str, budget_secs: int, ledger: Ledger) -> None:
        self.path = os.path.join(root, "%s.lock" % worker)
        self.budget_secs = budget_secs
        self.ledger = ledger
        self.held = False
        os.makedirs(root, exist_ok=True)

    def acquire(self) -> None:
        try:
            os.mkdir(self.path)
            self.held = True
            return
        except FileExistsError:
            pass
        age = time.time() - os.stat(self.path).st_mtime
        if age > self.budget_secs + LOCK_GRACE_SECS:
            self.ledger.append("WARN", reason="stale-lock-reclaim", age_secs=int(age))
            os.rmdir(self.path)
            os.mkdir(self.path)
            self.held = True
            return
        raise _Skip("lock held (age %ds)" % int(age))

    def release(self) -> None:
        if self.held:
            try:
                os.rmdir(self.path)
            except OSError:
                pass
            self.held = False


def _sha256(path: str) -> Tuple[str, str]:
    """Read a law file from its canonical path; return (text, sha256). §6."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _free_mb(path: str) -> int:
    st = os.statvfs(path)
    return int(st.f_bavail * st.f_frsize / (1024 * 1024))


def _dig(obj: object, dot_path: str) -> object:
    for key in dot_path.split("."):
        if not isinstance(obj, dict) or key not in obj:
            raise KeyError(dot_path)
        obj = obj[key]
    return obj


def _probe_queue(worker: Worker) -> Optional[int]:
    """GET the roster's queue probe; return ready count, or None if unprobed.

    Unreachable desk is an ERROR, not a skip — someone must notice (§4).
    """
    if not worker.queue_url:
        return None
    try:
        with urllib.request.urlopen(worker.queue_url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise InfraError("desk unreachable at %s: %s" % (worker.queue_url, exc))
    try:
        return int(_dig(data, worker.queue_count_key))  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        raise InfraError(
            "queue probe %s has no int at %r" % (worker.queue_url, worker.queue_count_key)
        )


def _preflight(worker: Worker) -> Optional[int]:
    if _free_mb(worker.workdir) < worker.min_free_mb:
        raise _Skip("low disk (<%dMB free)" % worker.min_free_mb)
    # a roster env PATH governs both this check and the spawn (subprocess
    # resolves argv[0] against the env it is given) — keep them in agreement
    if shutil.which(worker.command[0], path=worker.env.get("PATH")) is None:
        raise _Skip("CLI %r not installed" % worker.command[0])
    count = _probe_queue(worker)
    if count is not None and count <= 0:
        raise _Skip("queue empty")
    return count


def _fetch_secret(worker: Worker) -> Optional[str]:
    """§5 — dispatch-time keychain read. Absent item = fall back silently."""
    if not worker.keychain_service:
        return None
    out = subprocess.run(
        ["security", "find-generic-password", "-s", worker.keychain_service, "-w"],
        capture_output=True, text=True,
    )
    secret = out.stdout.strip()
    return secret or None


def _predirty_snapshot(worker: Worker, run_dir: str) -> Optional[str]:
    """§7 — record what was already dirty before this shift."""
    probe = subprocess.run(
        ["git", "-C", worker.workdir, "status", "--porcelain=v1"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        return None  # not a git workdir; nothing to guard
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, "predirty-%s.txt" % worker.name)
    lines = []
    for raw in probe.stdout.splitlines():
        entry = raw[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        lines.append(entry)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))
    return path


def _build_env(worker: Worker, predirty: Optional[str], secret: Optional[str]) -> Dict[str, str]:
    env = dict(os.environ)
    for var in SCRUBBED_ENV:
        env.pop(var, None)
    # §7 predirty is engine-controlled per-shift state, not inheritable: a
    # nested shift (or a suite run inside a live shift — WorkForce dispatches
    # its own mechanic) must never see the parent's snapshot. Scrub both the
    # default var and the worker's custom one, then set this shift's below.
    env.pop(PREDIRTY_ENV, None)
    if worker.predirty_env:
        env.pop(worker.predirty_env, None)
    env[IDENTITY_ENV] = worker.identity
    if predirty:
        env[worker.predirty_env or PREDIRTY_ENV] = predirty
    env.update(worker.env)
    if secret and worker.keychain_env:
        env[worker.keychain_env] = secret
    return env


def _usage_from_output(out_path: str, offset: int, fields: Dict[str, str]) -> Dict[str, Union[str, int, float]]:
    """Consumption telemetry: dig roster-configured dot-paths
    out of the pass's output. Scan the bounded tail reversed; take the last
    JSON-object line from which at least one configured path resolves (oc-37:
    some CLIs emit a non-usage terminal event after the usage-bearing one).
    Vendor specifics live in the roster's ``usage_fields`` — the engine stays
    host-neutral. Telemetry must never fail a shift: any problem returns {}
    and the DONE line simply carries no usage keys."""
    if not fields:
        return {}
    try:
        with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            chunk = fh.read(2 * 1024 * 1024)  # a usage blob lives near the end; bound the read
        for line in reversed(chunk.splitlines()):
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                cand = json.loads(line)
            except ValueError:
                continue
            if not isinstance(cand, dict):
                continue
            out: Dict[str, Union[str, int, float]] = {}
            for key, dot_path in fields.items():
                if key in ("rc", "on_pass", "secs", "dry_run"):
                    continue  # engine-owned DONE keys; a colliding config is ignored, not fatal
                try:
                    val = _dig(cand, dot_path)
                except KeyError:
                    continue
                if isinstance(val, (int, float)) or isinstance(val, str):
                    out[key] = val
            if out:
                return out  # first (≥1 path) object wins; keep walking if this line was empty
        return {}
    except Exception:
        return {}


def _build_argv(worker: Worker, prompt_text: str) -> List[str]:
    argv: List[str] = []
    for token in worker.command:
        if "{model}" in token:
            if not worker.model:
                # unpinned = explicit roster choice; drop the token AND its
                # paired flag — the shell idiom ${MODEL:+--model "$MODEL"}
                # drops both, and an orphaned --model eats the next flag
                # (incident: claude parsed '-p' as the model, 2026-07-14)
                if argv and argv[-1].startswith("-") and token == "{model}":
                    argv.pop()
                continue
            token = token.replace("{model}", worker.model)
        token = token.replace("{prompt_text}", prompt_text)
        token = token.replace("{prompt_path}", worker.prompt)  # CLIs that take a file
        argv.append(token)
    return argv


def dispatch(worker: Worker, local_root: str, dry_run: bool = False) -> int:
    """Run one shift for one worker. Returns the process exit code (0/1)."""
    ledger = Ledger(os.path.join(local_root, "ledger"), worker.name)
    lock = _Lock(os.path.join(local_root, "locks"), worker.name, worker.budget_secs, ledger)
    try:
        queue_count = _preflight(worker)
        lock.acquire()
    except _Skip as skip:
        ledger.append("SKIP", reason=str(skip))
        return 0
    except InfraError as exc:
        ledger.append("ERROR", reason=str(exc))
        return 1

    try:
        try:
            contract_text, contract_sha = _sha256(worker.contract)
            prompt_text, prompt_sha = _sha256(worker.prompt)
        except OSError as exc:
            ledger.append("ERROR", reason="law unreadable: %s" % exc)
            return 1

        predirty = None if dry_run else _predirty_snapshot(worker, os.path.join(local_root, "run"))
        secret = None if dry_run else _fetch_secret(worker)
        env = _build_env(worker, predirty, secret)
        argv = _build_argv(worker, prompt_text)

        ledger.append(
            "START",
            identity=worker.identity, kind=worker.kind,
            model=worker.model or "default", budget_secs=worker.budget_secs,
            max_passes=worker.max_passes,
            queue=("?" if queue_count is None else queue_count),
            contract_sha=contract_sha, prompt_sha=prompt_sha,
            dry_run=int(dry_run),
        )

        if dry_run:
            ledger.append("DONE", dry_run=1, argv_head=argv[0], argv_len=len(argv))
            return 0

        # §6 multi-pass: fresh passes while budget, ceiling, queue, and
        # progress allow. The deadline is the whole-shift budget, so the
        # lock's stale-reclaim math is untouched by pass count.
        # Worker output goes to a per-shift file (§8 log tail — an agent that
        # dies in 2s must leave its stderr behind); truncated each shift.
        out_dir = os.path.join(local_root, "run")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "%s.out" % worker.name)
        outfh = open(out_path, "w", encoding="utf-8")
        deadline = time.monotonic() + worker.budget_secs
        prev_queue = queue_count
        passes = 0
        while True:
            remain = deadline - time.monotonic()
            outfh.write("--- pass %d ---\n" % (passes + 1))
            outfh.flush()
            pass_offset = outfh.tell()  # telemetry reads this pass's slice only
            pass_t0 = time.monotonic()
            proc = subprocess.Popen(argv, cwd=worker.workdir, env=env,
                                    stdout=outfh, stderr=subprocess.STDOUT)
            try:
                rc = proc.wait(timeout=max(remain, 0.1))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                ledger.append("ERROR", reason="killed at budget",
                              budget_secs=worker.budget_secs, on_pass=passes + 1)
                return 1
            if rc != 0:
                ledger.append("ERROR", reason="agent exit", rc=rc, on_pass=passes + 1)
                return 1
            passes += 1
            outfh.flush()
            usage = _usage_from_output(out_path, pass_offset, worker.usage_fields)
            ledger.append("DONE", rc=0, on_pass=passes,
                          secs=int(time.monotonic() - pass_t0), **usage)

            if passes >= worker.max_passes:
                ledger.append("STOP", reason=("single-pass complete" if worker.max_passes == 1
                                              else "max passes (%d)" % worker.max_passes))
                return 0
            if deadline - time.monotonic() < worker.min_pass_secs:
                ledger.append("STOP", reason="budget floor (<%ds left)" % worker.min_pass_secs)
                return 0
            try:
                now_queue = _probe_queue(worker)
            except InfraError as exc:
                # work already done this shift — a dead probe stops, not errors
                ledger.append("STOP", reason="queue unprobed between passes: %s" % exc)
                return 0
            if now_queue is None or now_queue <= 0:
                ledger.append("STOP", reason="queue empty")
                return 0
            if prev_queue is not None and now_queue >= prev_queue:
                # Multipass still stops — do not burn the budget on a flat or
                # restocked queue. Reason distinguishes true sit (count
                # unchanged) from restock (count rose: closed work + filed
                # follow-ups, peer growth, etc.). Health only wedges on
                # "no progress" so restock does not ship-cut a productive
                # default-lane coordinator (morgan 2026-07-16 class).
                if now_queue > prev_queue:
                    reason = "restocked (%s -> %s)" % (prev_queue, now_queue)
                else:
                    reason = "no progress (%s -> %s)" % (prev_queue, now_queue)
                ledger.append("STOP", reason=reason)
                return 0
            prev_queue = now_queue
    finally:
        try:
            outfh.close()
        except (NameError, OSError):
            pass  # dry-run/preflight paths never opened it
        lock.release()
