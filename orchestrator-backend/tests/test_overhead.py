"""Overhead — how much of a plan is provledger itself (DP phase 2, Task 6; H1 / H4)."""
import io
import json
import sqlite3
from pathlib import Path

import pytest

from orchestrator import db, hooks, plan_metrics

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "docs" / "perf-baseline.json"


def _feed(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def test_command_head_is_recorded_for_bash_only(tmp_path, monkeypatch, capsys):
    dbp = tmp_path / "o.db"; monkeypatch.setenv("ORCH_DB", str(dbp)); monkeypatch.setenv("PROVLEDGER_HOOK_ERRORS", str(tmp_path / "e.log"))
    _feed(monkeypatch, {"session_id": "s", "cwd": "/x", "tool_name": "Bash", "tool_input": {"command": "bash scripts/run-step.sh /tmp/in.json   \n && echo " + "y" * 100}})
    hooks.main(["PostToolUse"])
    _feed(monkeypatch, {"session_id": "s", "cwd": "/x", "tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "bash scripts/run-step.sh"}})
    hooks.main(["PostToolUse"])
    assert capsys.readouterr().out == ""
    rows = sqlite3.connect(str(dbp)).execute("SELECT tool_name, command_head FROM tool_call_log ORDER BY id").fetchall()
    assert rows[0][0] == "Bash" and rows[0][1].startswith("bash scripts/run-step.sh /tmp/in.json") and len(rows[0][1]) == 80 and "\n" not in rows[0][1]
    assert rows[1] == ("Edit", None)


@pytest.mark.parametrize("head,bucket", [
    ("bash /home/x/skills/executing-plans/scripts/run-step.sh /tmp/in.json", "orchestration"),
    ("bash skills/writing-plans/scripts/publish-plan.sh plan.json", "orchestration"),
    ("$PY skills/update-project-state-graph/scripts/review_run.py --plan-id p", "orchestration"),
    ("bash scripts/deviate.sh /tmp/d.json", "orchestration"),
    ("bash scripts/reason-fill.sh /tmp/r.json", "provenance"),
    ("provledger why pkg.m.load", "provenance"),
    ("bash hooks/anchor_check.sh < in.json", "provenance"),
    ("uv run analyzer run --project x", "provenance"),
    ("pytest orchestrator-backend -q", None),
    ("git commit -m 'run-steps are fun'", "orchestration"),        # a substring match is a substring match — documented, not hidden
    ("ls -la", None),
    ("", None),
])
def test_classification(head, bucket):
    assert plan_metrics.classify(head) == bucket


def _plan(conn, pid, project="proj", created="2026-09-15 10:00:00", completed="2026-09-15 11:00:00", ic=None, hl=None):
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at, completed_at, impact_context, headline_json) "
                 "VALUES (?, 'g', 'COMPLETED', ?, 'declared', ?, ?, ?, ?)", (pid, project, created, completed, ic, hl))
    conn.commit()


def _call(conn, at, tool="Bash", head=None, cwd="/repo"):
    conn.execute("INSERT INTO tool_call_log (session_id, cwd, tool_name, command_head, at) VALUES ('s', ?, ?, ?, ?)", (cwd, tool, head, at))


def test_overhead_buckets_ratio_and_context_tokens(conn, monkeypatch):
    monkeypatch.setattr(plan_metrics.psg_bridge, "repo_for", lambda p: "/repo")
    pack = {"approx_tokens": 400, "shown": 2, "targets": []}
    doc = {"findings": [{"id": "active_constraint:nk_a:1", "layer": "self", "kind": "active_constraint", "tier": "asserted", "severity": "blocking",
                         "text": "keep paid only (human, 2026-09-01)", "anchor": "pkg.m.load", "evidence": {"reason_id": 1}, "hard": False, "response": None}],
           "summary": {"targets": 1, "layers": 2, "findings": 1, "blocking": 1, "warning": 0, "info": 0, "unanswered": 1, "shown": 2, "adopted": 0}, "hints": []}
    _plan(conn, "P1", ic=json.dumps({"pack": pack}), hl=json.dumps(doc))
    for i, head in enumerate(["bash scripts/run-step.sh a", "bash scripts/reason-fill.sh r", "pytest -q", "git commit", None, "bash scripts/complete-step.sh c"]):
        _call(conn, f"2026-09-15 10:{i:02d}:00", head=head)
    _call(conn, "2026-09-15 10:30:00", tool="Edit")                                  # not Bash: never in a bucket
    _call(conn, "2026-09-15 10:31:00", head="bash scripts/run-step.sh x", cwd="/elsewhere")   # another checkout: not this plan's
    _call(conn, "2026-09-15 12:00:00", head="bash scripts/run-step.sh late")                  # outside the window
    conn.execute("INSERT INTO change_reason (project, plan_id, node_key, kind, role, statement, occurred_at, recorded_by, tier, hash) VALUES ('proj', 'ledger', 'nk_a', 'organizational', 'constraint', 'keep paid only', '2026-09-01 00:00:00', 'human', 'asserted', 'h')")
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment, injected_chars, at) VALUES (1, 'proj', 'P1', 'edit', 300, '2026-09-15 10:40:00')")
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment, injected_chars, at) VALUES (1, 'proj', NULL, 'edit', 100, '2026-09-15 10:41:00')")
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment, injected_chars, at) VALUES (1, 'proj', 'P1', 'edit', 4000, '2026-09-15 13:00:00')")   # after the window
    conn.commit()
    o = plan_metrics.overhead(conn, "P1")
    assert (o["total_calls"], o["orchestration_calls"], o["provenance_calls"]) == (7, 2, 1)
    assert o["orchestration_ratio"] == round(2 / 7, 4) and o["provenance_ratio"] == round(1 / 7, 4) and o["overhead_ratio"] == round(2 / 7 + 1 / 7, 4)
    assert o["context"] == {"pack": 400, "headline": len(__import__("orchestrator.checks", fromlist=["render"]).render(doc)) // 4, "injected": 100}
    assert o["context_overhead_tokens"] == 400 + o["context"]["headline"] + 100 and o["measured"] is True
    _plan(conn, "P0", created="2026-09-14 10:00:00", completed="2026-09-14 11:00:00")
    e = plan_metrics.overhead(conn, "P0")
    assert e["measured"] is False and e["overhead_ratio"] is None and e["context_overhead_tokens"] == 0


def test_baseline_carries_the_two_new_columns_and_keeps_superpowers_only(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(plan_metrics.psg_bridge, "repo_for", lambda p: "/repo")
    _plan(conn, "P1", ic=json.dumps({"pack": {"approx_tokens": 120}}))
    _call(conn, "2026-09-15 10:01:00", head="bash scripts/run-step.sh a"); _call(conn, "2026-09-15 10:02:00", head="pytest"); conn.commit()
    data = plan_metrics.baseline(conn)
    assert data["overhead"]["overhead_ratio"]["median"] == 0.5 and data["overhead"]["context_overhead_tokens"]["median"] == 120.0
    assert data["overhead"]["plans"]["P1"]["provenance_ratio"] == 0.0
    path = tmp_path / "b.json"
    path.write_text(json.dumps({"superpowers_only": {"method": "hand", "tool_calls": 11}}))
    plan_metrics.write_baseline(path, data)
    back = json.loads(path.read_text())
    assert back["superpowers_only"] == {"method": "hand", "tool_calls": 11} and "overhead" in back


def _recent(conn, n=5):
    return [r[0] for r in conn.execute("SELECT plan_id FROM Plans WHERE status='COMPLETED' ORDER BY created_at DESC LIMIT ?", (n,))]


def test_h1_overhead_ratio(capsys):
    """The last 5 completed plans of the real DB: provenance ≤ 10 %, overhead ≤ 35 %. No measured data → skip, out loud."""
    real = Path.home() / "skill-workspace" / "orchestrator.db"
    if not real.exists():
        print("SKIP-NOTE: H1 — no ~/skill-workspace/orchestrator.db on this machine")
        pytest.skip("no orchestrator.db")
    conn = db.open_db(real)
    try:
        rows = [plan_metrics.overhead(conn, p) for p in _recent(conn)]
    finally:
        conn.close()
    measured = [r for r in rows if r["measured"]]
    if not measured:
        print("SKIP-NOTE: H1 — none of the last 5 completed plans has a tool_call_log row in its window (the hook loads at session start); nothing to assert yet")
        pytest.skip("no measured plans")
    for r in measured:
        assert r["provenance_ratio"] <= 0.10, r
        assert r["overhead_ratio"] <= 0.35, r


def test_h4_context_overhead(capsys):
    """The last 5 completed plans: context_overhead_tokens ≤ 3000 each. Plans without a pack → skip, out loud."""
    real = Path.home() / "skill-workspace" / "orchestrator.db"
    if not real.exists():
        print("SKIP-NOTE: H4 — no ~/skill-workspace/orchestrator.db on this machine")
        pytest.skip("no orchestrator.db")
    conn = db.open_db(real)
    try:
        rows = [plan_metrics.overhead(conn, p) for p in _recent(conn)]
    finally:
        conn.close()
    with_pack = [r for r in rows if r["context_overhead_tokens"] > 0]
    if not with_pack:
        print("SKIP-NOTE: H4 — none of the last 5 completed plans carries a context pack / headline / injection; nothing to assert yet")
        pytest.skip("no context data")
    for r in with_pack:
        assert r["context_overhead_tokens"] <= 3000, r


def test_baseline_file_has_the_overhead_section_and_the_superpowers_only_reference():
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert "overhead" in data and {"overhead_ratio", "context_overhead_tokens"} <= set(data["overhead"])
    sp = data["superpowers_only"]
    assert sp["method"] and "tool_calls" in sp and "note" in sp          # a reference number, never an assertion
