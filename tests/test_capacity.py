"""oc-34 wedge detection + oc-35 consumption telemetry — token-free."""

import json
import os

from workforce.engine import _usage_from_output
from workforce.ledger import Ledger, parse_shifts
from workforce.board import _worker_health
from workforce.roster import Worker


def _worker(tmp_path, **over):
    spec = dict(
        name="w", workdir=str(tmp_path), contract="c", prompt="p",
        identity="w", command=["/bin/sh", "-c", "true"],
    )
    spec.update(over)
    return Worker(**spec)


# ── engine: _usage_from_output ────────────────────────────────────────────

FIELDS = {"tok_in": "usage.input_tokens", "tok_out": "usage.output_tokens",
          "cost_usd": "total_cost_usd"}


def test_usage_extracted_from_last_json_line(tmp_path):
    out = tmp_path / "w.out"
    out.write_text("--- pass 1 ---\nchatter\n" + json.dumps(
        {"result": "done", "usage": {"input_tokens": 120, "output_tokens": 34},
         "total_cost_usd": 0.0125}) + "\n")
    got = _usage_from_output(str(out), 0, FIELDS)
    assert got == {"tok_in": 120, "tok_out": 34, "cost_usd": 0.0125}


def test_usage_respects_pass_offset(tmp_path):
    out = tmp_path / "w.out"
    pass1 = "--- pass 1 ---\n" + json.dumps(
        {"usage": {"input_tokens": 999, "output_tokens": 999}}) + "\n"
    out.write_text(pass1 + "--- pass 2 ---\nplain text only\n")
    got = _usage_from_output(str(out), len(pass1.encode("utf-8")), FIELDS)
    assert got == {}


def test_usage_never_raises(tmp_path):
    assert _usage_from_output(str(tmp_path / "missing.out"), 0, FIELDS) == {}
    out = tmp_path / "w.out"
    out.write_text("{not json}\n")
    assert _usage_from_output(str(out), 0, FIELDS) == {}
    assert _usage_from_output(str(out), 0, {}) == {}


def test_usage_skips_engine_owned_keys(tmp_path):
    out = tmp_path / "w.out"
    out.write_text(json.dumps({"secs": 1, "usage": {"input_tokens": 5}}) + "\n")
    got = _usage_from_output(str(out), 0, {"secs": "secs", "tok_in": "usage.input_tokens"})
    assert got == {"tok_in": 5}


def test_usage_falls_back_to_earlier_json_line(tmp_path):
    """oc-37: terminal non-usage event after a usage-bearing object (codex)."""
    out = tmp_path / "w.out"
    usage_line = json.dumps(
        {"type": "token_count", "usage": {"input_tokens": 42, "output_tokens": 7},
         "total_cost_usd": 0.003})
    terminal = json.dumps(
        {"type": "task_complete", "last_agent_message": "done", "duration_ms": 1200})
    out.write_text("--- pass 1 ---\nchatter\n%s\n%s\n" % (usage_line, terminal))
    got = _usage_from_output(str(out), 0, FIELDS)
    assert got == {"tok_in": 42, "tok_out": 7, "cost_usd": 0.003}


def test_usage_no_matching_paths_returns_empty(tmp_path):
    """oc-37: every JSON line present, none carries a configured path → {}."""
    out = tmp_path / "w.out"
    out.write_text(
        json.dumps({"type": "item", "text": "hello"}) + "\n"
        + json.dumps({"type": "task_complete", "last_agent_message": "bye"}) + "\n")
    assert _usage_from_output(str(out), 0, FIELDS) == {}


# ── ledger: parse_shifts usage read model ─────────────────────────────────

def _ledger_with(tmp_path, lines):
    led = Ledger(str(tmp_path / "ledger"), "w")
    with open(led.path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return led


def test_parse_shifts_sums_usage(tmp_path):
    led = _ledger_with(tmp_path, [
        "2026-07-15T01:00:00Z START identity=w kind=lane model=m budget_secs=100 max_passes=4 queue=2",
        "2026-07-15T01:02:00Z DONE rc=0 on_pass=1 secs=110 tok_in=100 tok_out=20 cost_usd=0.01",
        "2026-07-15T01:04:00Z DONE rc=0 on_pass=2 secs=100 tok_in=50 tok_out=10 cost_usd=0.02",
        "2026-07-15T01:04:01Z STOP reason=\"queue empty\"",
    ])
    (shift,) = parse_shifts(led.tail(20))
    assert shift["usage"]["secs"] == 210
    assert shift["usage"]["tok_in"] == 150
    assert shift["usage"]["tok_out"] == 30
    assert round(shift["usage"]["cost_usd"], 4) == 0.03


def test_parse_shifts_usage_absent_is_empty(tmp_path):
    led = _ledger_with(tmp_path, [
        "2026-07-15T01:00:00Z START identity=w kind=lane budget_secs=100 max_passes=1 queue=1",
        "2026-07-15T01:02:00Z DONE rc=0 on_pass=1",
        "2026-07-15T01:02:01Z STOP reason=\"single-pass complete\"",
    ])
    (shift,) = parse_shifts(led.tail(20))
    assert shift["usage"] == {}


# ── board: wedge health (the kai incident class) ──────────────────────────

WEDGE_LINES = []
for i in range(3):
    WEDGE_LINES += [
        "2026-07-15T0%d:10:00Z START identity=w kind=lane budget_secs=100 max_passes=4 queue=1" % (i + 1),
        "2026-07-15T0%d:10:30Z DONE rc=0 on_pass=1 secs=30" % (i + 1),
        "2026-07-15T0%d:10:30Z STOP reason=\"no progress (1 -> 1)\"" % (i + 1),
    ]


def test_wedged_when_no_progress_and_queue_nonempty(tmp_path):
    _ledger_with(tmp_path, WEDGE_LINES)
    h = _worker_health(str(tmp_path), _worker(tmp_path), queue="1")
    assert h["cls"] == "wedged"
    assert "no-progress" in h["why"]


def test_not_wedged_once_queue_drains(tmp_path):
    _ledger_with(tmp_path, WEDGE_LINES)
    h = _worker_health(str(tmp_path), _worker(tmp_path), queue="0")
    assert h["cls"] == "ok"


def test_not_wedged_when_recent_shift_progressed(tmp_path):
    lines = WEDGE_LINES + [
        "2026-07-15T04:10:00Z START identity=w kind=lane budget_secs=100 max_passes=4 queue=2",
        "2026-07-15T04:20:00Z DONE rc=0 on_pass=1 secs=600",
        "2026-07-15T04:20:01Z STOP reason=\"queue empty\"",
    ]
    _ledger_with(tmp_path, lines)
    h = _worker_health(str(tmp_path), _worker(tmp_path), queue="1")
    assert h["cls"] == "ok"


def test_not_wedged_when_shifts_restocked(tmp_path):
    """Close-1 + file-follow-ups grows the probe — not a sitting worker."""
    lines = []
    for i in range(3):
        lines += [
            "2026-07-15T0%d:10:00Z START identity=w kind=lane budget_secs=100 max_passes=4 queue=%d"
            % (i + 1, 50 + i),
            "2026-07-15T0%d:10:30Z DONE rc=0 on_pass=1 secs=30" % (i + 1),
            "2026-07-15T0%d:10:30Z STOP reason=\"restocked (%d -> %d)\""
            % (i + 1, 50 + i, 51 + i),
        ]
    _ledger_with(tmp_path, lines)
    h = _worker_health(str(tmp_path), _worker(tmp_path), queue="53")
    assert h["cls"] == "ok"
