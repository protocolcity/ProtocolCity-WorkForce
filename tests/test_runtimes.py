"""Tests for workforce.runtimes — detect(), staffing_pool(), and the CLI verb."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import cli as cli_mod
from workforce.runtimes import KNOWN_RUNTIMES, detect, staffing_pool


# ── detect() ──────────────────────────────────────────────────────────────────


def test_detect_returns_entry_for_every_known_runtime(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    result = detect()
    assert set(result.keys()) == set(KNOWN_RUNTIMES)


def test_detect_resolves_installed_cli(monkeypatch):
    def fake_which(name, path=None):
        return "/usr/local/bin/%s" % name if name == "claude" else None

    monkeypatch.setattr("shutil.which", fake_which)
    result = detect()
    assert result["claude"] == "/usr/local/bin/claude"
    assert result["codex"] is None


def test_detect_not_installed_returns_none(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    result = detect()
    assert all(v is None for v in result.values())


def test_detect_forwards_env_path_to_which(monkeypatch):
    seen_paths = []

    def fake_which(name, path=None):
        seen_paths.append(path)
        return None

    monkeypatch.setattr("shutil.which", fake_which)
    detect(env_path="/custom/bin")
    assert all(p == "/custom/bin" for p in seen_paths)
    assert len(seen_paths) == len(KNOWN_RUNTIMES)


# ── staffing_pool() ───────────────────────────────────────────────────────────


def test_staffing_pool_without_roster_has_empty_workers():
    detected = {name: "/bin/%s" % name for name in KNOWN_RUNTIMES}
    pool = staffing_pool(detected, roster=None)
    assert len(pool) == len(KNOWN_RUNTIMES)
    for entry in pool:
        assert entry["workers"] == []


def test_staffing_pool_preserves_known_runtime_order():
    detected = {name: None for name in KNOWN_RUNTIMES}
    pool = staffing_pool(detected)
    assert [e["cli"] for e in pool] == KNOWN_RUNTIMES


def test_staffing_pool_cross_refs_roster(tmp_path):
    from workforce import roster as roster_mod

    (tmp_path / "CONTRACT.md").write_text("")
    (tmp_path / "prompt.md").write_text("")
    roster_data = {
        "workers": {
            "otto": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "otto",
                "command": ["claude", "-p", "workers/otto/prompt.md"],
            },
            "grok-worker": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "grok-worker",
                "command": ["grok", "--prompt", "@prompt.md"],
            },
        }
    }
    rpath = tmp_path / "roster.json"
    rpath.write_text(json.dumps(roster_data))
    r = roster_mod.load(str(rpath))

    detected = {name: "/bin/%s" % name for name in KNOWN_RUNTIMES}
    pool = staffing_pool(detected, roster=r)
    by_cli = {e["cli"]: e for e in pool}

    assert "otto" in by_cli["claude"]["workers"]
    assert "grok-worker" in by_cli["grok"]["workers"]
    assert by_cli["codex"]["workers"] == []
    assert by_cli["cursor"]["workers"] == []


def test_staffing_pool_handles_full_path_command(tmp_path):
    """command[0] = full path (e.g. /usr/local/bin/claude) is resolved by basename."""
    from workforce import roster as roster_mod

    (tmp_path / "CONTRACT.md").write_text("")
    (tmp_path / "prompt.md").write_text("")
    roster_data = {
        "workers": {
            "otto": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "otto",
                "command": ["/usr/local/bin/claude", "-p", "prompt.md"],
            },
        }
    }
    rpath = tmp_path / "roster.json"
    rpath.write_text(json.dumps(roster_data))
    r = roster_mod.load(str(rpath))

    detected = {name: "/usr/local/bin/%s" % name for name in KNOWN_RUNTIMES}
    pool = staffing_pool(detected, roster=r)
    by_cli = {e["cli"]: e for e in pool}
    assert "otto" in by_cli["claude"]["workers"]


# ── CLI verb ──────────────────────────────────────────────────────────────────


def test_cli_runtimes_succeeds_without_roster(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    monkeypatch.setenv("WORKFORCE_ROSTER", str(tmp_path / "nonexistent.json"))
    rc = cli_mod.main(["runtimes"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "RUNTIME STAFFING POOL" in captured.out
    # All known runtimes appear in output even when not installed.
    for name in KNOWN_RUNTIMES:
        assert name in captured.out


def test_cli_runtimes_shows_installed_path(capsys, monkeypatch, tmp_path):
    def fake_which(name, path=None):
        return "/usr/local/bin/claude" if name == "claude" else None

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setenv("WORKFORCE_ROSTER", str(tmp_path / "nonexistent.json"))
    rc = cli_mod.main(["runtimes"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "/usr/local/bin/claude" in captured.out
    assert "not installed" in captured.out  # the other runtimes


def test_scene_model_includes_runtimes(monkeypatch, tmp_path):
    """scene_model() carries a 'runtimes' key with one entry per known runtime."""
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    from workforce import board as board_mod

    model = board_mod.scene_model(str(tmp_path))
    assert "runtimes" in model
    pool = model["runtimes"]
    assert len(pool) == len(KNOWN_RUNTIMES)
    assert [e["cli"] for e in pool] == KNOWN_RUNTIMES
    assert all(e["path"] is None for e in pool)
    assert all(e["workers"] == [] for e in pool)


def test_cli_runtimes_employment_status(capsys, monkeypatch, tmp_path):
    """Installed CLI employed by a roster worker shows 'employed by: <name>'."""
    from workforce import roster as roster_mod

    (tmp_path / "CONTRACT.md").write_text("")
    (tmp_path / "prompt.md").write_text("")
    roster_data = {
        "workers": {
            "otto": {
                "workdir": str(tmp_path),
                "contract": str(tmp_path / "CONTRACT.md"),
                "prompt": str(tmp_path / "prompt.md"),
                "identity": "otto",
                "command": ["claude", "-p", "workers/otto/prompt.md"],
            },
        }
    }
    rpath = tmp_path / "roster.json"
    rpath.write_text(json.dumps(roster_data))

    monkeypatch.setattr("shutil.which",
                        lambda name, path=None: "/usr/local/bin/%s" % name if name == "claude" else None)
    rc = cli_mod.main(["--file", str(rpath), "runtimes"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "employed by: otto" in captured.out
    assert "available" not in captured.out.split("employed")[0]  # claude is not "available"
