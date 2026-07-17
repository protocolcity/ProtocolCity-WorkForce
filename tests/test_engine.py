"""Engine mechanics — every RUNNER_SPEC MUST, verified without burning tokens."""

import json
import os
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import engine  # noqa: E402
from workforce.roster import Roster, RosterError, Worker, load  # noqa: E402


def make_worker(tmp_path, **over):
    workdir = tmp_path / "hood"
    workdir.mkdir(exist_ok=True)
    contract = tmp_path / "CONTRACT.md"
    prompt = tmp_path / "prompt.md"
    contract.write_text("# contract v1\n")
    prompt.write_text("do one slice\n")
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"ok": True, "count": 3}))
    spec = dict(
        name="tester", workdir=str(workdir), contract=str(contract),
        prompt=str(prompt), identity="tester-id",
        command=["/bin/sh", "-c", "exit 0"],
        queue_url="file://" + str(queue), budget_secs=5, min_free_mb=1,
    )
    spec.update(over)
    return Worker(**spec)


def ledger_text(tmp_path):
    p = tmp_path / "local" / "ledger" / "tester.log"
    return p.read_text() if p.exists() else ""


def local(tmp_path):
    return str(tmp_path / "local")


def test_dispatch_success_records_start_done_with_law_hashes(tmp_path):
    w = make_worker(tmp_path)
    assert engine.dispatch(w, local(tmp_path)) == 0
    text = ledger_text(tmp_path)
    assert "START" in text and "DONE" in text and "STOP" in text
    assert "contract_sha=" in text and "prompt_sha=" in text  # §6 law-version pins
    assert "identity=tester-id" in text and "queue=3" in text


def test_law_change_changes_pinned_hash(tmp_path):
    w = make_worker(tmp_path)
    engine.dispatch(w, local(tmp_path))
    first = [l for l in ledger_text(tmp_path).splitlines() if " START " in l][0]
    (tmp_path / "CONTRACT.md").write_text("# contract v2 — tightened\n")
    engine.dispatch(w, local(tmp_path))
    second = [l for l in ledger_text(tmp_path).splitlines() if " START " in l][1]
    sha = lambda line: [t for t in line.split() if t.startswith("contract_sha=")][0]
    assert sha(first) != sha(second)


def test_dry_run_spawns_nothing(tmp_path):
    marker = tmp_path / "ran"
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "touch %s" % marker])
    assert engine.dispatch(w, local(tmp_path), dry_run=True) == 0
    assert not marker.exists()
    assert "dry_run=1" in ledger_text(tmp_path)


def test_empty_queue_skips_cleanly(tmp_path):
    w = make_worker(tmp_path)
    (tmp_path / "queue.json").write_text(json.dumps({"ok": True, "count": 0}))
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "SKIP" in ledger_text(tmp_path) and "queue empty" in ledger_text(tmp_path)


def test_unreachable_desk_is_infra_error(tmp_path):
    w = make_worker(tmp_path, queue_url="file://" + str(tmp_path / "missing.json"))
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "ERROR" in ledger_text(tmp_path) and "desk unreachable" in ledger_text(tmp_path)


def test_missing_cli_skips(tmp_path):
    w = make_worker(tmp_path, command=["definitely-not-a-real-cli-xyz"])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "not installed" in ledger_text(tmp_path)


def test_lock_held_skips(tmp_path):
    w = make_worker(tmp_path)
    lock_dir = tmp_path / "local" / "locks"
    lock_dir.mkdir(parents=True)
    (lock_dir / "tester.lock").mkdir()
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "lock held" in ledger_text(tmp_path)


def test_budget_kill_is_infra_error(tmp_path):
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "sleep 30"], budget_secs=1)
    assert engine.dispatch(w, local(tmp_path)) == 1
    assert "killed at budget" in ledger_text(tmp_path)


def test_lock_released_after_shift(tmp_path):
    w = make_worker(tmp_path)
    engine.dispatch(w, local(tmp_path))
    assert not (tmp_path / "local" / "locks" / "tester.lock").exists()


def test_child_env_scrubbed_and_identity_set(tmp_path, monkeypatch):
    monkeypatch.setenv("TP_PRODUCT", "leaky")
    monkeypatch.setenv("TP_DEFAULT_PRODUCT", "leaky")
    monkeypatch.setenv("TP_AGENT_ID", "wrong-identity")
    out = tmp_path / "env.txt"
    w = make_worker(tmp_path, command=["/bin/sh", "-c", "env > %s" % out])
    assert engine.dispatch(w, local(tmp_path)) == 0
    env_text = out.read_text()
    assert "TP_AGENT_ID=tester-id" in env_text            # §2: exactly one identity
    assert "TP_PRODUCT=" not in env_text                   # §2: no ambient scope
    assert "TP_DEFAULT_PRODUCT=" not in env_text


def test_predirty_snapshot_written_for_git_workdir(tmp_path):
    w = make_worker(tmp_path)
    subprocess.run(["git", "-C", w.workdir, "init", "-q"], check=True)
    (tmp_path / "hood" / "wip.txt").write_text("citizen WIP\n")
    engine.dispatch(w, local(tmp_path))
    snap = tmp_path / "local" / "run" / "predirty-tester.txt"
    assert snap.exists() and "wip.txt" in snap.read_text()


def test_model_pin_substitution_and_unpinned_drop(tmp_path):
    w = make_worker(tmp_path, model="test-model-1")
    argv = engine._build_argv(w, "P")
    assert argv == ["/bin/sh", "-c", "exit 0"]
    w2 = make_worker(tmp_path, command=["cli", "--model", "{model}", "-p", "{prompt_text}"],
                     model="m1")
    assert engine._build_argv(w2, "brief") == ["cli", "--model", "m1", "-p", "brief"]
    w3 = make_worker(tmp_path, command=["cli", "--model={model}", "-p", "{prompt_text}"], model="")
    assert engine._build_argv(w3, "brief") == ["cli", "-p", "brief"]
    # the paired-flag form must not leave an orphaned --model behind
    # (incident 2026-07-14: claude parsed '-p' as the model name)
    w4 = make_worker(tmp_path, command=["cli", "--model", "{model}", "-p", "{prompt_text}"],
                     model="")
    assert engine._build_argv(w4, "brief") == ["cli", "-p", "brief"]
    w5 = make_worker(tmp_path, command=["cli", "--model", "{model}", "-p", "{prompt_text}"],
                     model="m9")
    assert engine._build_argv(w5, "brief") == ["cli", "--model", "m9", "-p", "brief"]


def test_roster_env_path_governs_cli_preflight(tmp_path):
    """A roster env PATH must satisfy preflight — the daemon runs on a bare
    launchd PATH, so vendor CLI locations are roster data like every seam."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "fake-vendor-cli"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    w = make_worker(tmp_path, command=["fake-vendor-cli"])
    assert engine.dispatch(w, local(tmp_path)) == 0
    assert "not installed" in ledger_text(tmp_path)  # not on ambient PATH
    w2 = make_worker(tmp_path, command=["fake-vendor-cli"],
                     env={"PATH": "%s:/usr/bin:/bin" % bindir})
    assert engine.dispatch(w2, local(tmp_path)) == 0
    assert "DONE" in ledger_text(tmp_path)           # found via roster PATH


def test_roster_rejects_duplicate_identity(tmp_path):
    p = tmp_path / "roster.json"
    w = {"workdir": ".", "contract": "c", "prompt": "p", "identity": "same",
         "command": ["x"]}
    p.write_text(json.dumps({"workers": {"a": w, "b": dict(w)}}))
    with pytest.raises(RosterError, match="one worker, one identity"):
        load(str(p))


def test_roster_rejects_unknown_fields_and_bad_kind(tmp_path):
    p = tmp_path / "roster.json"
    p.write_text(json.dumps({"workers": {"a": {
        "workdir": ".", "contract": "c", "prompt": "p", "identity": "i",
        "command": ["x"], "sudo": True}}}))
    with pytest.raises(RosterError, match="unknown fields"):
        load(str(p))
