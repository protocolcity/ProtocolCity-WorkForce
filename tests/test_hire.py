"""Hire write path — papers + roster row (STAFFING §2)."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from workforce import hire as hire_mod
from workforce.roster import RosterError, load


def test_slugify():
    assert hire_mod.slugify("Neo Market") == "neo-market"
    assert hire_mod.slugify("  Ames  ") == "ames"


def test_hire_rejects_office_staff(tmp_path):
    hood = tmp_path / "hood"
    hood.mkdir()
    with pytest.raises(RosterError, match="permanent"):
        hire_mod.hire(
            name="office-steward",
            workdir=str(hood),
            role="Steward",
            base=str(tmp_path),
            roster_path=str(tmp_path / "local" / "roster.json"),
            dry_run=True,
        )


def test_hire_rejects_you(tmp_path):
    hood = tmp_path / "hood"
    hood.mkdir()
    with pytest.raises(RosterError, match="permanent"):
        hire_mod.hire(
            name="you",
            workdir=str(hood),
            role="Owner",
            base=str(tmp_path),
            roster_path=str(tmp_path / "local" / "roster.json"),
            dry_run=True,
        )


def test_hire_plants_papers_and_arms_roster(tmp_path):
    hood = tmp_path / "gridfinity"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    # Seed a minimal valid roster so load() after hire succeeds with peers
    roster_path.write_text(json.dumps({
        "workers": {
            "seed": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "seed",
                "command": ["true"],
            }
        }
    }))
    (hood / "c.md").write_text("# c\n")
    (hood / "p.md").write_text("p\n")

    result = hire_mod.hire(
        name="Neo",
        workdir=str(hood),
        role="Market Analyst",
        project="gridfinity",
        base=str(tmp_path),
        roster_path=str(roster_path),
        plant=True,
    )
    assert result["ok"] is True
    assert result["armed"] is True
    assert result["worker"]["name"] == "neo"
    assert result["worker"]["display"] == "Neo · Market Analyst"
    contract = hood / "workers" / "neo" / "CONTRACT.md"
    prompt = hood / "workers" / "neo" / "prompt.md"
    assert contract.is_file() and prompt.is_file()
    raw = json.loads(roster_path.read_text())
    assert "neo" in raw["workers"]
    assert raw["workers"]["neo"]["identity"] == "neo"
    assert "gridfinity" in raw["workers"]["neo"]["queue_url"]
    # Live load still validates
    r = load(path=str(roster_path), base=str(tmp_path))
    assert "neo" in r.workers
    assert any("Desk" in s or "PROCESS" in s for s in result["next_steps"])


def test_hire_dry_run_does_not_write_roster(tmp_path):
    hood = tmp_path / "hood"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    roster_path.write_text(json.dumps({
        "workers": {
            "seed": {
                "kind": "job",
                "workdir": str(hood),
                "contract": str(hood / "c.md"),
                "prompt": str(hood / "p.md"),
                "identity": "seed",
                "command": ["true"],
            }
        }
    }))
    (hood / "c.md").write_text("# c\n")
    (hood / "p.md").write_text("p\n")

    result = hire_mod.hire(
        name="Kai",
        workdir=str(hood),
        role="Patrol",
        base=str(tmp_path),
        roster_path=str(roster_path),
        dry_run=True,
    )
    assert result["ok"] is True
    assert result["armed"] is False
    raw = json.loads(roster_path.read_text())
    assert "kai" not in raw["workers"]
    # Papers still planted so the citizen can review before arming
    assert (hood / "workers" / "kai" / "CONTRACT.md").is_file()


def test_hire_rejects_duplicate(tmp_path):
    hood = tmp_path / "hood"
    hood.mkdir()
    roster_path = tmp_path / "local" / "roster.json"
    (tmp_path / "local").mkdir()
    hire_mod.hire(
        name="Riley",
        workdir=str(hood),
        role="Clerk",
        base=str(tmp_path),
        roster_path=str(roster_path),
    )
    with pytest.raises(RosterError, match="already"):
        hire_mod.hire(
            name="Riley",
            workdir=str(hood),
            role="Clerk",
            base=str(tmp_path),
            roster_path=str(roster_path),
        )
