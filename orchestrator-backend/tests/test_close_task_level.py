"""The close of a reviewed plan, per task (task-level redesign, step 3a).

The close used to try rules R0-R5 on every node the plan touched and write a
reason (or an ask / silent verdict) per node, then backstop every node nobody
explained with its own `unstated` row. Now the agent writes the task's reason
before the close, and the close only fills the gap: one `unstated` task reason
when nobody wrote one. R6 (failed steps become rejected paths) and the
external-trigger log run as before. The session close (Stop hook) follows the
same rule."""
import json
import sys
from pathlib import Path

import pytest

from orchestrator import api, constraints, db, provenance as pv, reasons, session

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402
from test_reasons import _close, _psg, _seed_registered_plan  # noqa: E402


@pytest.fixture
def psg_with_plan(tmp_path):
    return _psg(tmp_path)


@pytest.fixture
def registry(tmp_path, psg_with_plan):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": "proj", "repo": "/x", "db_path": psg_with_plan, "commit_sha": "c"}]}))
    return str(p)


def _reason_rows(conn, plan_id):
    return [tuple(r) for r in conn.execute(
        "SELECT node_key, role, tier, rule_id FROM change_reason WHERE plan_id = ? ORDER BY id", (plan_id,))]


def _verdicts(conn, plan_id):
    return [tuple(r) for r in conn.execute(
        "SELECT path, rule_id, verdict FROM trigger_log WHERE plan_id = ? ORDER BY id", (plan_id,))]


def test_the_close_writes_no_rule_reasons_and_one_unstated_task_when_nobody_explained(conn, registry):
    plan_id, _ = _seed_registered_plan(conn, "P1", registry=registry)
    pv.insert_utterance(conn, session_id="s", project="proj", plan_id=plan_id, occurred_at="2026-09-15 10:00:00",
                        text="please keep load_orders on fiscal weeks")          # R0 would have quoted this
    constraints.record_constraint(conn, project="proj", subjects=["nk_b"], statement="clean keeps test rows out",
                                  rationale="x")                                 # R5 would have cited this
    out = _close(conn, plan_id, registry)
    assert db.get_plan(conn, plan_id)["status"] == "COMPLETED"
    assert _reason_rows(conn, plan_id) == [(None, "reason", "unstated", None),
                                           ("nk_b", "constraint", "derived", "constraint_bypassed")]   # as before
    assert out["unstated_backstopped"] == 1
    assert [v for v in _verdicts(conn, plan_id) if v[0] == "code"] == []        # no auto / ask / silent per node


def test_a_task_reason_written_before_the_close_is_the_only_reason(conn, registry, psg_with_plan):
    plan_id, _ = _seed_registered_plan(conn, "P1", registry=registry)
    reasons.fill_tasks(conn, project="proj", plan_id=plan_id, run_id=2, psg_db_path=psg_with_plan,
                       tasks=[{"interpretation": "finance asked for fiscal weeks"}])
    out = _close(conn, plan_id, registry)
    assert _reason_rows(conn, plan_id) == [(None, "reason", "asserted", None)]
    assert out["unstated_backstopped"] == 0


def test_rejected_paths_and_the_external_trigger_log_run_as_before(conn, registry):
    plan_id, _ = _seed_registered_plan(conn, "P1", steps=("CODE: rewrite load_orders() filter",), registry=registry,
                                       deviation=("TimeSeriesSplit leaked future rows", "use GroupKFold"))
    _close(conn, plan_id, registry)
    rows = _reason_rows(conn, plan_id)
    assert ("nk_a", "rejected_path", "derived", "R6") in rows
    assert ("code", "R6", "auto") in _verdicts(conn, plan_id)


def test_the_session_close_follows_the_same_rule(conn, tmp_path):
    graph = _psg(tmp_path, plan_id=session.session_plan_id("s9"))
    out = session.evaluate_session(conn, project="proj", session_id="s9", psg_db_path=graph, commit=True)
    pid = session.session_plan_id("s9")
    assert out["unstated"] == 1 and _reason_rows(conn, pid) == [(None, "reason", "unstated", None)]
    assert [v for v in _verdicts(conn, pid) if v[0] == "code"] == []
