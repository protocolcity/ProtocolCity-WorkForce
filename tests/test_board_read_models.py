"""The board's read models — shift parsing, law stack, health — no HTTP."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce.board import _contract_rules, _law_stack, _worker_queue  # noqa: E402
from workforce.ledger import parse_shifts  # noqa: E402
from workforce.roster import Worker  # noqa: E402

LEDGER = """\
2026-07-14T01:40:00Z SKIP reason="CLI 'claude' not installed"
2026-07-14T02:10:00Z START identity=x kind=lane model=m budget_secs=1500 queue=1 contract_sha=aa prompt_sha=bb dry_run=0
2026-07-14T02:15:02Z DONE rc=0
2026-07-14T02:15:02Z STOP reason="single-pass complete"
2026-07-14T03:00:00Z START identity=x kind=lane model=m budget_secs=1500 max_passes=4 queue=80 contract_sha=aa prompt_sha=bb dry_run=0
2026-07-14T03:04:00Z DONE rc=0 on_pass=1
2026-07-14T03:08:00Z DONE rc=0 on_pass=2
2026-07-14T03:08:01Z STOP reason="no progress (79 -> 79)"
2026-07-14T03:30:00Z START identity=x kind=lane model=m budget_secs=5 queue=3 contract_sha=aa prompt_sha=bb dry_run=0
2026-07-14T03:30:05Z ERROR reason="killed at budget" budget_secs=5 on_pass=1
"""


def test_parse_shifts_groups_and_orders_newest_first():
    shifts = parse_shifts(LEDGER)
    assert [s["outcome"] for s in shifts] == ["error", "ok", "ok", "skip"]
    multi = shifts[1]
    assert multi["passes"] == 2 and multi["queue"] == "80"
    assert "no progress" in multi["reason"]
    assert shifts[3]["reason"].startswith("CLI 'claude'")


def test_parse_shifts_running_shift_stays_open():
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = LEDGER + "%s START identity=x kind=lane queue=2 dry_run=0\n" % now
    shifts = parse_shifts(text)
    assert shifts[0]["outcome"] == "running" and shifts[0]["end_ts"] == ""


def test_parse_shifts_dry_run_closes_at_done():
    text = ("2026-07-14T04:00:00Z START identity=x kind=lane queue=2 dry_run=1\n"
            "2026-07-14T04:00:00Z DONE dry_run=1 argv_head=cli argv_len=3\n")
    s = parse_shifts(text)[0]
    assert s["outcome"] == "ok" and s["dry_run"] and s["reason"] == "dry-run"


def make_worker(tmp_path):
    hood = tmp_path / "city" / "hood"
    hood.mkdir(parents=True)
    (tmp_path / "city" / "AGENTS.md").write_text("# city law\n")
    (hood / "AGENTS.md").write_text("# neighborhood law\n")
    contract = hood / "CONTRACT.md"
    contract.write_text("# contract\n\n## Lane\nwork lane:x tickets\n\n"
                        "## Never touch\nsecrets\nprod\n\n## Notes\nnot a rule\n")
    prompt = hood / "prompt.md"
    prompt.write_text("brief\n")
    return Worker(name="x", workdir=str(hood), contract=str(contract),
                  prompt=str(prompt), identity="x", command=["true"])


def test_worker_queue_reads_roster_dot_path(tmp_path):
    probe = tmp_path / "queue.json"
    probe.write_text('{"column_counts": {"backlog": 91}}')
    w = make_worker(tmp_path)
    w.queue_url = probe.as_uri()
    w.queue_count_key = "column_counts.backlog"
    assert _worker_queue(w) == "91"


def test_law_stack_walks_conventions(tmp_path):
    stack = _law_stack(make_worker(tmp_path))
    labels = [(e["label"], os.path.basename(e["path"])) for e in stack]
    assert ("city rules", "AGENTS.md") in labels
    assert labels[-2:] == [("contract", "CONTRACT.md"), ("prompt", "prompt.md")]
    hood_agents = [e for e in stack if e["label"] == "neighborhood rules"]
    assert len(hood_agents) == 1 and hood_agents[0]["sha"]


def test_crashed_shift_detected_past_budget_grace():
    text = ("2026-07-14T00:00:00Z START identity=x kind=lane budget_secs=300 "
            "queue=5 dry_run=0\n")  # hours ago, no terminal event
    s = parse_shifts(text)[0]
    assert s["outcome"] == "crashed"
    assert "no terminal event" in s["reason"]


def test_workplaces_grouped_from_roster(tmp_path):
    import json as _json
    from workforce.board import _workplaces
    from workforce.roster import Roster

    w1 = make_worker(tmp_path)
    hood2 = tmp_path / "city" / "hood2"
    hood2.mkdir()
    w2 = Worker(name="y", workdir=str(hood2), contract=str(w1.contract),
                prompt=str(w1.prompt), identity="y", command=["true"],
                queue_url="http://127.0.0.1:9999/api?x=1")
    roster = Roster(workers={"x": w1, "y": w2}, path="test")
    local = tmp_path / "local"
    local.mkdir()
    (local / "platforms.json").write_text(_json.dumps(
        {"platforms": [], "workplace_names": {"hood2": "PublicName"}}))
    groups = _workplaces(roster, str(local),
                         {"x": {"cls": "ok"}, "y": {"cls": "err"}},
                         {"x": "3", "y": "?"})
    by_label = {g["label"]: g for g in groups}
    assert set(by_label) == {"hood", "PublicName"}
    assert by_label["hood"]["queue"] == 3 and by_label["hood"]["worst"] == "ok"
    assert by_label["PublicName"]["worst"] == "err"
    assert "http://127.0.0.1:9999" in by_label["PublicName"]["desks"]


def test_contract_rules_picks_rule_headings(tmp_path):
    w = make_worker(tmp_path)
    rules = _contract_rules(w.contract)
    titles = [r["title"] for r in rules]
    assert "Lane" in titles and "Never touch" in titles
    assert "Notes" not in titles
