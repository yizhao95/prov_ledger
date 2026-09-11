"""orchestrator.reasons — close-time closed-form reason capture (E1-1 / E1-2 / E1-3)."""
import json
import sys
from pathlib import Path

import pytest

from orchestrator import api, db, psg_bridge, reasons

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402


def _psg(tmp_path, plan_id="P1", keys=(("nk_a", "pkg.m.load_orders"), ("nk_b", "pkg.m.clean"), ("nk_c", "pkg.m.load_orders:df.amount"))):
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    for i, (k, qn) in enumerate(keys, 1):
        ps.add_snapshot(c, 1, k, qn.replace("load_orders", "load"))
        ps.add_event(c, 1, i, "node_added", k)
    ps.add_run(c, 2, plan_id=plan_id, step_id=f"{plan_id}-REVIEW.1")
    for i, (k, qn) in enumerate(keys, 1):
        ps.add_snapshot(c, 2, k, qn, ntype="column" if ":" in qn else "function")
        ps.add_event(c, 2, i, "node_changed", k, '{"changed": ["struct_sig"]}')
    c.commit(); c.close()
    return str(path)


@pytest.fixture
def psg_with_plan(tmp_path):
    return _psg(tmp_path)


@pytest.fixture
def registry(tmp_path, psg_with_plan):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": "proj", "repo": "/x", "db_path": psg_with_plan, "commit_sha": "c"}]}))
    return str(p)


@pytest.fixture
def registry_without_db(tmp_path):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": "proj", "repo": "/x", "db_path": str(tmp_path / "absent.db"), "commit_sha": "c"}]}))
    return str(p)


def _seed_registered_plan(conn, plan_id, *, steps=("CODE: work on proj",), registry=None, deviation=None):
    """A plan mentioning project 'proj', parked in NEEDS_REVIEW with its child
    REVIEW.1 IN_PROGRESS (the state the review agent works in). With
    `deviation=(justification, sub_step)` step A FAILED, was deviated from and
    its sub-step COMPLETED — a recovered failure, the way rejected paths arise."""
    db.insert_plan(conn, plan_id, f"improve proj pipeline: {plan_id}")
    for i, desc in enumerate(steps):
        status = "FAILED" if (deviation and i == 0) else "COMPLETED"
        db.insert_step(conn, f"{plan_id}-{chr(65 + i)}", plan_id, desc, i, status=status)
    if deviation:
        out = api.evaluate_and_update_plan(conn, deviation_detected=True, target_step_id=f"{plan_id}-A",
                                           justification=deviation[0], new_sub_steps=[deviation[1]])
        assert out.get("accepted", True), out
        db.update_step_status(conn, f"{plan_id}-A.1", "COMPLETED", set_completed=True)
    review = db.insert_review_step(conn, plan_id)
    out = api.review_and_complete(conn, plan_id, registry_path=registry)
    assert out["needs_agent_review"] is True, out
    db.update_step_status(conn, out["review_child_step_id"], "IN_PROGRESS", set_started=True)
    return plan_id, review


def _close(conn, plan_id, registry):
    db.update_step_status(conn, f"{plan_id}-REVIEW.1", "COMPLETED", set_completed=True)
    return api.review_and_complete(conn, plan_id, registry_path=registry)


def test_slots_and_checklist_are_closed_form(conn, psg_with_plan):
    slots = reasons.slots_for_plan(conn, "proj", "P1", psg_with_plan)
    assert [s["node_key"] for s in slots] == ["nk_a", "nk_b", "nk_c"]
    text = reasons.checklist_text(slots)
    assert "3 个数据点" in text and "unstated" in text
    for i, s in enumerate(slots, 1):
        assert f"{i}. {s['qualified_name']}" in text and s["node_key"] in text
    assert "column" in text and "node_changed" in text
    assert "不需要说明原因" in reasons.checklist_text([])
    # a filled slot disappears from the checklist
    reasons.fill(conn, project="proj", plan_id="P1", run_id=2, reasons=[{"node_key": "nk_b", "text": "dedupe"}], psg_db_path=psg_with_plan)
    assert [s["node_key"] for s in reasons.slots_for_plan(conn, "proj", "P1", psg_with_plan)] == ["nk_a", "nk_c"]


def test_e1_1_close_creates_one_reason_slot_per_changed_node_and_records_unstated(conn, psg_with_plan, registry):
    plan_id, review = _seed_registered_plan(conn, "P1", registry=registry)
    r = reasons.fill(conn, project="proj", plan_id=plan_id, run_id=2, psg_db_path=psg_with_plan,
                     reasons=[{"node_key": "nk_a", "text": "financial weeks"}, {"node_key": "nk_b", "text": "unstated"}])
    assert r == {"filled": 1, "unstated": 1, "unknown_keys": []}
    out = _close(conn, plan_id, registry)
    rows = db.get_node_reasons(conn, plan_id=plan_id)
    assert {x["node_key"]: x["text"] for x in rows if x["kind"] == "reason"} == {"nk_a": "financial weeks", "nk_b": None, "nk_c": None}
    assert {x["source"] for x in rows if x["node_key"] == "nk_c"} == {"system"}       # backstop, visible, not fabricated
    assert {x["tier"] for x in rows if x["node_key"] == "nk_c"} == {"derived"}
    assert {x["tier"] for x in rows if x["node_key"] == "nk_a"} == {"stated"}
    assert out["plan_status"] == "COMPLETED" and out["unstated_backstopped"] == 1        # never blocked
    assert db.get_plan(conn, plan_id)["status"] == "COMPLETED" and db.get_step(conn, review)["status"] == "COMPLETED"
    assert reasons.unstated_ratio(conn, "proj", plan_id) == {"slots": 3, "unstated": 2, "ratio": round(2 / 3, 4)}


def test_fill_rejects_keys_outside_the_change_set(conn, psg_with_plan):
    r = reasons.fill(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg_with_plan,
                     reasons=[{"node_key": "nk_a", "text": "ok"}, {"node_key": "nk_zzz", "text": "x"}])
    assert r == {"filled": 0, "unstated": 0, "unknown_keys": ["nk_zzz"]}
    assert db.get_node_reasons(conn, plan_id="P1") == []           # all-or-nothing


def test_e1_2_deviation_justification_lands_on_changed_node(conn, psg_with_plan, registry):
    plan_id, review = _seed_registered_plan(conn, "P1", steps=("CODE: rewrite load_orders filter",), registry=registry,
                                            deviation=("TimeSeriesSplit leaked future rows", "use GroupKFold"))
    _close(conn, plan_id, registry)
    rp = [r for r in db.get_node_reasons(conn, plan_id=plan_id) if r["kind"] == "rejected_path"]
    assert len(rp) == 1 and rp[0]["node_key"] == "nk_a"              # nk_a's qualified_name ends with load_orders
    assert rp[0]["tier"] == "asserted" and "TimeSeriesSplit" in rp[0]["text"] and rp[0]["step_id"] == f"{plan_id}-A"


def test_rejected_path_without_anchor_is_kept_unanchored(conn, psg_with_plan, registry):
    plan_id, _ = _seed_registered_plan(conn, "P1", steps=("CODE: tidy the notebook",), registry=registry,
                                       deviation=("the notebook kernel kept dying", "restart"))
    _close(conn, plan_id, registry)
    rp = [r for r in db.get_node_reasons(conn, plan_id=plan_id) if r["kind"] == "rejected_path"]
    assert len(rp) == 1 and rp[0]["node_key"] is None and "kernel" in rp[0]["text"]


def test_e1_3_reason_survives_rename(conn, psg_with_plan):
    db.insert_node_reason(conn, node_key="nk_a", project="proj", run_id=1, plan_id="P0", kind="reason",
                          text="fiscal weeks", source="agent", tier="stated")
    key = psg_bridge.node_key_of(psg_with_plan, "pkg.m.load_orders")       # renamed since P0 (was pkg.m.load)
    assert key == "nk_a" and db.get_node_reasons(conn, node_key=key)[0]["text"] == "fiscal weeks"
    assert psg_bridge.node_key_of(psg_with_plan, "pkg.m.load") == "nk_a"


def test_reasons_never_written_when_graph_missing(conn, registry_without_db):
    plan_id, review = _seed_registered_plan(conn, "P1", registry=registry_without_db)
    out = _close(conn, plan_id, registry_without_db)
    assert out["plan_status"] == "COMPLETED" and out["unstated_backstopped"] == 0
    assert db.get_node_reasons(conn, plan_id=plan_id) == []
    assert "state graph unavailable" in (db.get_step(conn, review)["log_context"] or "")


def test_close_is_idempotent_for_reasons(conn, psg_with_plan, registry):
    plan_id, _ = _seed_registered_plan(conn, "P1", registry=registry)
    _close(conn, plan_id, registry)
    n = len(db.get_node_reasons(conn, plan_id=plan_id))
    api.review_and_complete(conn, plan_id, registry_path=registry)       # already terminal: no-op
    assert len(db.get_node_reasons(conn, plan_id=plan_id)) == n == 3
