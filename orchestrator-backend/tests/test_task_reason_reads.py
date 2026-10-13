"""A task's reason reaches the nodes the task changed (task-level redesign, step 3a).

A plan's close records why the task was done once, with no node on the row. A
node reaches it through its history in the state graph: the runs that changed
the node carry the plan id, and the plan's task reason that covers the node
(the task it was assigned to, else the task that names no nodes) is shown with
the node's own records — by every read a person runs, end to end, and marked
as the reason of that plan's task. A node the plan only matched, or never
touched, does not get it. Per-node rows written before migration 036 read as
before."""
import sys
from pathlib import Path

import pytest

from orchestrator import context_pack, db, evidence, provenance as pv, reasons, why
from orchestrator.ask import facts, receipts

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

MAIN = "the reviewer fixed the evaluation window at two quarters"
SIDE = "an outage inside the window skewed the daily average"
OLD = "exports stay daily because finance reconciles daily"
NODES = {"nk_a": "pkg.m.load_orders", "nk_b": "pkg.m.clean", "nk_c": "pkg.m.weekly_totals", "nk_d": "pkg.m.export"}


@pytest.fixture
def world(conn, tmp_path):
    """P changed load_orders and clean (two tasks: clean was the side one) and
    only matched export; Q changed weekly_totals and nobody said why; export
    carries a per-node reason from before 036."""
    graph = tmp_path / "proj-state-graph.db"
    c = ps.build(graph)
    ps.add_run(c, 1, plan_id="P0")
    for i, (k, qn) in enumerate(NODES.items(), 1):
        ps.add_snapshot(c, 1, k, qn)
        ps.add_event(c, 1, i, "node_added", k)
    ps.add_run(c, 2, plan_id="P", step_id="P-REVIEW.1")
    for i, k in enumerate(("nk_a", "nk_b"), 1):
        ps.add_snapshot(c, 2, k, NODES[k], struct_sig="s2")
        ps.add_event(c, 2, i, "node_changed", k, '{"changed": ["struct_sig"]}')
    ps.add_event(c, 2, 3, "node_matched", "nk_d", '{"via": "qualname"}')
    ps.add_run(c, 3, plan_id="Q", step_id="Q-REVIEW.1")
    ps.add_snapshot(c, 3, "nk_c", NODES["nk_c"], struct_sig="s3")
    ps.add_event(c, 3, 1, "node_changed", "nk_c", '{"changed": ["struct_sig"]}')
    c.commit()
    c.close()
    graph = str(graph)
    for pid in ("P0", "P", "Q"):
        db.insert_plan(conn, pid, f"goal of {pid}", project="proj", project_source="declared")
    old = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_d", kind="technical",
                           interpretation=OLD, recorded_by="agent")
    out = reasons.fill_tasks(conn, project="proj", plan_id="P", run_id=2, psg_db_path=graph,
                             bundle="both answer the same review comment",
                             tasks=[{"interpretation": MAIN}, {"interpretation": SIDE, "nodes": ["pkg.m.clean"]}])
    reasons.backstop_unstated(conn, project="proj", plan_id="Q", psg_db_path=graph, commit=True)
    q_row = conn.execute("SELECT id FROM change_reason WHERE plan_id = 'Q'").fetchone()[0]
    main, side = out["reason_ids"]
    return {"graph": graph, "main": main, "side": side, "old": old, "q": q_row}


def _why(conn, world, qn, **kw):
    return why.why(conn, project="proj", target=qn, psg_db_path=world["graph"], record=False, **kw)


def test_why_shows_the_task_reason_of_the_plan_that_changed_the_node(conn, world):
    text = _why(conn, world, "pkg.m.load_orders")["text"]
    assert MAIN in text and "task reason of P" in text and SIDE not in text


def test_with_two_tasks_a_node_gets_only_the_task_it_was_assigned_to(conn, world):
    text = _why(conn, world, "pkg.m.clean")["text"]
    assert SIDE in text and MAIN not in text


def test_a_node_the_plan_only_matched_keeps_its_own_records_and_gets_nothing_of_the_plan(conn, world):
    text = _why(conn, world, "pkg.m.export")["text"]
    assert OLD in text and MAIN not in text and SIDE not in text


def test_an_unexplained_task_is_pending_on_the_node_it_changed(conn, world):
    out = _why(conn, world, "pkg.m.weekly_totals")
    assert out["doc"]["summary"]["pending"] == 1
    assert f"#{world['q']}" in _why(conn, world, "pkg.m.weekly_totals", pending_only=True)["text"]
    assert [r["id"] for r in why.pending(conn, project="proj", anchors=["nk_c"], psg_db_path=world["graph"])] == [world["q"]]
    assert _why(conn, world, "pkg.m.load_orders")["doc"]["summary"]["pending"] == 0


def test_the_fact_table_and_the_receipts_timeline_carry_the_task_reason(conn, world):
    ft = facts.facts(conn, world["graph"], ["pkg.m.load_orders"], project="proj")
    text = facts.render(ft)
    assert MAIN in text and "task reason of P" in text and SIDE not in text
    lines = [e["text"] for e in receipts.timeline(conn, ft)]
    assert any(MAIN in t and "task" in t for t in lines), lines


def test_the_context_pack_carries_the_task_reason_marked_with_its_plan(conn, world):
    pack = context_pack.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=world["graph"], record=False)
    (tp,) = pack.targets
    assert [(r["text"], r.get("task_of")) for r in tp.reasons] == [(MAIN, "P")]


def test_evidence_slots_hang_a_source_on_the_task_reason(conn, world):
    slots = {s["node_key"]: s for s in evidence.slots_for_plan(conn, project="proj", plan_id="P", psg_db_path=world["graph"])}
    assert (slots["nk_a"]["reason_id"], slots["nk_b"]["reason_id"]) == (world["main"], world["side"])


def test_a_corrected_task_reason_is_replaced_by_its_correction(conn, world):
    fix = pv.insert_reason(conn, project="proj", plan_id="P", node_key=None, kind="technical",
                           interpretation="the window is two quarters because the reviewer asked", recorded_by="human")
    pv.supersede(conn, world["main"], fix)
    text = _why(conn, world, "pkg.m.load_orders")["text"]
    assert "because the reviewer asked" in text and f"corrects #{world['main']}" in text and MAIN not in text
