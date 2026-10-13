"""Close-time reasons per task (task-level redesign, step 3a).

A plan is a task, and its close records why the task was done — the root, what
the agent saw, what it did not choose — once, not once per changed function.
When a plan did several things, each gets its own reason, the changed nodes are
assigned to one of them (only assigned, never written about one by one), and one
sentence says why they share a plan. The nodes reach their task's reason through
the state graph's history; nothing is copied onto them."""
import sys
from pathlib import Path

import pytest

from orchestrator import db, provenance as pv, reasons, roots

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

PLAN = "P1"
NODES = {"nk_a": "pkg.m.load_orders", "nk_b": "pkg.m.clean", "nk_c": "pkg.m.weekly_totals"}


@pytest.fixture
def graph(tmp_path):
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_run(c, 2, plan_id=PLAN, step_id=f"{PLAN}-REVIEW.1")
    for i, (k, qn) in enumerate(NODES.items(), 1):
        ps.add_snapshot(c, 1, k, qn)
        ps.add_event(c, 1, i, "node_added", k)
        ps.add_snapshot(c, 2, k, qn, struct_sig="s2")
        ps.add_event(c, 2, i, "node_changed", k, '{"changed": ["struct_sig"]}')
    c.commit()
    c.close()
    return str(path)


@pytest.fixture
def plan(conn):
    db.insert_plan(conn, PLAN, "evaluate on the last two quarters", user_query="judge it on two quarters of traffic",
                   project="proj", project_source="declared")
    conn.commit()
    return PLAN


def _fill(conn, graph, tasks, bundle=None):
    return reasons.fill_tasks(conn, project="proj", plan_id=PLAN, run_id=2, tasks=tasks, bundle=bundle,
                              psg_db_path=graph)


def _task_rows(conn):
    return conn.execute("SELECT id, node_key, tier, interpretation FROM change_reason "
                        "WHERE plan_id = ? AND role = 'reason' ORDER BY id", (PLAN,)).fetchall()


def test_one_task_reason_covers_every_node_the_plan_changed(conn, graph, plan):
    out = _fill(conn, graph, [{"interpretation": "the reviewer fixed the window at two quarters"}])
    rows = _task_rows(conn)
    assert [(r["node_key"], r["tier"]) for r in rows] == [(None, "asserted")]
    assert out["tasks"] == 1 and out["asserted"] == 1 and out["unknown_nodes"] == []
    assert conn.execute("SELECT COUNT(*) FROM reason_node").fetchone()[0] == 0
    for k in NODES:
        assert reasons.covering_task_reasons(conn, k, [PLAN]) == [rows[0]["id"]]


def test_a_task_reason_may_quote_the_user_or_say_nobody_said(conn, graph, plan):
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id=PLAN, text="judge it on two quarters of traffic",
                            occurred_at="2026-09-15 10:00:00")
    _fill(conn, graph, [{"utterance_id": u, "span": [0, 12]}])
    assert _task_rows(conn)[0]["tier"] == "stated"
    db.insert_plan(conn, "P2", "g", project="proj", project_source="declared")
    out = reasons.fill_tasks(conn, project="proj", plan_id="P2", run_id=2, tasks=[{"unstated": True}], psg_db_path=graph)
    assert out["unstated"] == 1


def test_several_tasks_assign_the_nodes_and_say_why_they_share_a_plan(conn, graph, plan):
    out = _fill(conn, graph, [{"interpretation": "the window was set by the reviewer"},
                              {"interpretation": "an outage in the window skewed the average", "nodes": ["pkg.m.clean", "nk_c"]}],
                bundle="both answer the same review comment")
    main, side = [r["id"] for r in _task_rows(conn)]
    assert out["tasks"] == 2
    assert sorted(r[0] for r in conn.execute("SELECT node_key FROM reason_node WHERE reason_id = ?", (side,))) == ["nk_b", "nk_c"]
    assert conn.execute("SELECT basis FROM plan_bundle WHERE plan_id = ?", (PLAN,)).fetchone()[0] == "both answer the same review comment"
    assert reasons.covering_task_reasons(conn, "nk_b", [PLAN]) == [side]
    assert reasons.covering_task_reasons(conn, "nk_a", [PLAN]) == [main]          # not assigned: the main task


@pytest.mark.parametrize("tasks, bundle, says", [
    ([{"interpretation": "a"}, {"interpretation": "b", "nodes": ["nk_a"]}], None, "bundle"),
    ([{"interpretation": "a"}, {"interpretation": "b"}], "x", "every task but one names its nodes"),
    ([{"interpretation": "a", "nodes": ["nk_a"]}, {"interpretation": "b", "nodes": ["nk_a"]}], "x", "nk_a"),
    ([{"node_key": "nk_a", "interpretation": "x"}], None, "per task"),
    ([{"interpretation": "a", "unstated": True}], None, "exactly one"),
    ([], None, "at least one task"),
])
def test_a_close_that_cannot_be_recorded_writes_nothing(conn, graph, plan, tasks, bundle, says):
    with pytest.raises(reasons.FillInputError, match=says):
        _fill(conn, graph, tasks, bundle)
    assert _task_rows(conn) == [] and conn.execute("SELECT COUNT(*) FROM plan_bundle").fetchone()[0] == 0


def test_the_old_per_node_fill_is_refused_with_the_new_shape_named(conn, graph, plan):
    with pytest.raises(reasons.FillInputError, match="per task"):
        reasons.fill(conn, project="proj", plan_id=PLAN, run_id=2, reasons=[{"node_key": "nk_a", "unstated": True}])


def test_a_node_the_plan_did_not_change_is_reported_and_nothing_is_written(conn, graph, plan):
    out = _fill(conn, graph, [{"interpretation": "a"}, {"interpretation": "b", "nodes": ["pkg.m.nowhere"]}], bundle="x")
    assert out["unknown_nodes"] == ["pkg.m.nowhere"] and _task_rows(conn) == []


def test_because_records_what_the_task_leaned_on(conn, graph, plan):
    old = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical",
                           interpretation="finance reconciles weekly", recorded_by="agent")
    out = _fill(conn, graph, [{"interpretation": "kept the weekly grain", "because": [old]}])
    assert out["adopted"] == 1
    row = conn.execute("SELECT plan_id, node_key, via FROM influence WHERE reason_id = ?", (old,)).fetchone()
    assert tuple(row) == (PLAN, None, "reason_because")


def test_the_backstop_records_one_unstated_task_only_when_nobody_wrote_one(conn, graph, plan):
    assert reasons.backstop_unstated(conn, project="proj", plan_id=PLAN, psg_db_path=graph, commit=True) == 1
    rows = _task_rows(conn)
    assert [(r["node_key"], r["tier"]) for r in rows] == [(None, "unstated")]
    assert reasons.backstop_unstated(conn, project="proj", plan_id=PLAN, psg_db_path=graph, commit=True) == 0
    db.insert_plan(conn, "P2", "g", project="proj", project_source="declared")
    reasons.fill_tasks(conn, project="proj", plan_id="P2", run_id=2, tasks=[{"interpretation": "x"}], psg_db_path=graph)
    assert reasons.backstop_unstated(conn, project="proj", plan_id="P2", psg_db_path=graph, commit=True) == 0


def test_task_slots_give_the_agent_the_nodes_the_root_and_what_is_already_written(conn, graph, plan):
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id=PLAN, occurred_at="2026-09-15 10:00:00",
                            text="Judge the model on two quarters of traffic. Keep load_orders as it is.")
    pv.insert_utterance(conn, session_id="s", project="proj", plan_id=PLAN, occurred_at="2026-09-15 10:01:00",
                        text='<agent-message from="a1">\n[Subagent hand-back] load_orders changed\n</agent-message>')
    roots.record(conn, plan_id=PLAN, root={"kind": "new", "utterance_id": u})
    _fill(conn, graph, [{"interpretation": "first half"}])
    got = reasons.task_slots(conn, "proj", PLAN, graph, draft=True)
    assert sorted(n["qualified_name"] for n in got["nodes"]) == sorted(NODES.values())
    assert got["root"]["root_plan_id"] == PLAN and "two quarters" in got["root"]["words"]
    assert [t["text"] for t in got["tasks"]] == ["first half"]
    assert got["draft"] and all("hand-back" not in c["preview"] for c in got["draft"])
    assert got["draft"][0]["preview"].startswith("Keep load_orders")       # the sentence that names a changed node first


def test_a_corrected_task_reason_and_another_plans_reason_do_not_cover_the_node(conn, graph, plan):
    out = _fill(conn, graph, [{"interpretation": "first reading"}])
    first = _task_rows(conn)[0]["id"]
    fix = pv.insert_reason(conn, project="proj", plan_id=PLAN, node_key=None, kind="technical",
                           interpretation="second reading", recorded_by="agent")
    pv.supersede(conn, first, fix)
    assert reasons.covering_task_reasons(conn, "nk_a", [PLAN]) == [fix]
    assert reasons.covering_task_reasons(conn, "nk_a", ["P0"]) == []
    assert out["tasks"] == 1
