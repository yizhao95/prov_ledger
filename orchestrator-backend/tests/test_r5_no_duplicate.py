"""R5 stops writing one identical record per plan (DP phase 2d follow-up).

On this repo's own graph, `compute_etag` carried 16 `derived` reasons all
reading "covered by active constraint #1414" — one per plan that happened to
touch the node. The ledger was not wrong; it was repeating itself, and a node
page where 16 of 21 rows say the same sentence is a page nobody reads.

A constraint being in force is a FACT ABOUT THE NODE, not a fact about each
plan. So it is recorded once, and every later plan that meets it records that
it was surfaced — which is what read_hit is for.
"""
import sys
from pathlib import Path

import pytest

from orchestrator import constraints as oc, db, triggers

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402


@pytest.fixture
def graph(tmp_path):
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    for run, plan in ((1, "P1"), (2, "P2")):
        ps.add_run(c, run, plan_id=plan)
        ps.add_snapshot(c, run, "nk_a", "pkg.m.load_orders", struct_sig=f"s{run}")
        ps.add_event(c, run, 1, "node_changed", "nk_a", '{"changed": ["struct_sig"]}')
    c.commit(); c.close()
    return str(path)


def _plans(conn):
    for pid in ("P1", "P2"):
        conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source) "
                     "VALUES (?, 'g', 'IN_PROGRESS', 'proj', 'declared')", (pid,))
    oc.record_constraint(conn, project="proj", subjects=["nk_a"], statement="load_orders keeps paid orders only",
                         rationale="finance reconciles on paid orders", why_ref="docs/finance.md", why_visibility="shared")
    conn.commit()


def _derived(conn, node_key="nk_a"):
    return conn.execute("SELECT COUNT(*) FROM change_reason WHERE node_key = ? AND rule_id = 'R5'", (node_key,)).fetchone()[0]


def test_the_constraint_is_recorded_once_no_matter_how_many_plans_meet_it(conn, graph):
    _plans(conn)
    triggers.evaluate(conn, project="proj", plan_id="P1", psg_db_path=graph)
    assert _derived(conn) == 1, "the first plan records the coverage"
    triggers.evaluate(conn, project="proj", plan_id="P2", psg_db_path=graph)
    assert _derived(conn) == 1, "the second plan must not repeat the same sentence"


def test_the_later_plan_records_that_it_was_surfaced_instead(conn, graph):
    _plans(conn)
    triggers.evaluate(conn, project="proj", plan_id="P1", psg_db_path=graph)
    triggers.evaluate(conn, project="proj", plan_id="P2", psg_db_path=graph)
    hits = [dict(r) for r in conn.execute(
        "SELECT plan_id, moment, reason_id FROM read_hit WHERE plan_id = 'P2' AND moment = 'close'")]
    assert len(hits) == 1, f"P2 should record one close-time read_hit, got {hits}"
    rid = conn.execute("SELECT id FROM change_reason WHERE rule_id = 'R5'").fetchone()[0]
    assert hits[0]["reason_id"] == rid, "the read_hit must point at the record that already exists"


def test_the_trigger_log_still_says_the_rule_fired_and_points_at_the_record(conn, graph):
    _plans(conn)
    triggers.evaluate(conn, project="proj", plan_id="P1", psg_db_path=graph)
    triggers.evaluate(conn, project="proj", plan_id="P2", psg_db_path=graph)
    rows = [dict(r) for r in conn.execute(
        "SELECT plan_id, rule_id, verdict, basis FROM trigger_log WHERE plan_id = 'P2' AND rule_id = 'R5'")]
    assert len(rows) == 1 and rows[0]["verdict"] == "auto"
    rid = conn.execute("SELECT id FROM change_reason WHERE rule_id = 'R5'").fetchone()[0]
    assert str(rid) in rows[0]["basis"], f"the basis does not name the existing record: {rows[0]['basis']}"


def test_a_different_node_still_gets_its_own_record(conn, graph):
    """De-duplication is per node and per constraint — it must not swallow a
    genuinely separate coverage fact."""
    _plans(conn)
    oc.record_constraint(conn, project="proj", subjects=["nk_b"], statement="clean drops zero-quantity rows",
                         rationale="agreed with finance", why_ref="docs/finance.md", why_visibility="shared")
    conn.commit()
    triggers.evaluate(conn, project="proj", plan_id="P1", psg_db_path=graph)
    assert _derived(conn, "nk_a") == 1
