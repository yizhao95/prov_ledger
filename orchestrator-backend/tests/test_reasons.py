"""orchestrator.reasons — close-time closed-form reason capture (E1-1 / E1-2 / E1-3)."""
import json
import sys
from pathlib import Path

import pytest

from orchestrator import api, db, provenance as pv, psg_bridge, reasons

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
    reasons.fill(conn, project="proj", plan_id="P1", run_id=2, reasons=[{"node_key": "nk_b", "interpretation": "dedupe"}], psg_db_path=psg_with_plan)
    assert [s["node_key"] for s in reasons.slots_for_plan(conn, "proj", "P1", psg_with_plan)] == ["nk_a", "nk_c"]


def test_e1_1_close_creates_one_reason_slot_per_changed_node_and_records_unstated(conn, psg_with_plan, registry):
    plan_id, review = _seed_registered_plan(conn, "P1", registry=registry)
    r = reasons.fill(conn, project="proj", plan_id=plan_id, run_id=2, psg_db_path=psg_with_plan,
                     reasons=[{"node_key": "nk_a", "interpretation": "financial weeks"}, {"node_key": "nk_b", "unstated": True}])
    assert r == {"filled": 1, "stated": 0, "asserted": 1, "unstated": 1, "unknown_keys": []}
    out = _close(conn, plan_id, registry)
    from orchestrator import provenance as pv
    rows = [x for x in pv.reasons_for_plan(conn, plan_id) if x["role"] == "reason"]
    assert {x["node_key"]: x["interpretation"] for x in rows} == {"nk_a": "financial weeks", "nk_b": None, "nk_c": None}
    assert {x["recorded_by"] for x in rows if x["node_key"] == "nk_c"} == {"system"}       # backstop, visible, not fabricated
    assert {x["tier"] for x in rows if x["node_key"] == "nk_c"} == {"unstated"}
    assert {x["tier"] for x in rows if x["node_key"] == "nk_a"} == {"asserted"}             # free text is never stated
    assert out["plan_status"] == "COMPLETED" and out["unstated_backstopped"] == 1        # never blocked
    assert db.get_plan(conn, plan_id)["status"] == "COMPLETED" and db.get_step(conn, review)["status"] == "COMPLETED"
    assert reasons.unstated_ratio(conn, "proj", plan_id) == {"slots": 3, "unstated": 2, "ratio": round(2 / 3, 4)}


def test_fill_rejects_keys_outside_the_change_set(conn, psg_with_plan):
    r = reasons.fill(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg_with_plan,
                     reasons=[{"node_key": "nk_a", "interpretation": "ok"}, {"node_key": "nk_zzz", "interpretation": "x"}])
    assert r == {"filled": 0, "stated": 0, "asserted": 0, "unstated": 0, "unknown_keys": ["nk_zzz"]}
    assert conn.execute("SELECT COUNT(*) FROM change_reason").fetchone()[0] == 0           # all-or-nothing


def test_e1_2_deviation_justification_lands_on_changed_node(conn, psg_with_plan, registry):
    plan_id, review = _seed_registered_plan(conn, "P1", steps=("CODE: rewrite load_orders filter",), registry=registry,
                                            deviation=("TimeSeriesSplit leaked future rows", "use GroupKFold"))
    _close(conn, plan_id, registry)
    rp = [r for r in pv.reasons_for_plan(conn, plan_id, role="rejected_path")]
    assert len(rp) == 1 and rp[0]["node_key"] == "nk_a"              # nk_a's qualified_name ends with load_orders
    assert rp[0]["tier"] == "derived" and rp[0]["rule_id"] == "R6"     # DP phase 1: rule R6, not an agent assertion
    assert "TimeSeriesSplit" in rp[0]["interpretation"] and rp[0]["step_id"] == f"{plan_id}-A"


def test_rejected_path_without_anchor_is_kept_unanchored(conn, psg_with_plan, registry):
    plan_id, _ = _seed_registered_plan(conn, "P1", steps=("CODE: tidy the notebook",), registry=registry,
                                       deviation=("the notebook kernel kept dying", "restart"))
    _close(conn, plan_id, registry)
    rp = [r for r in pv.reasons_for_plan(conn, plan_id, role="rejected_path")]
    assert len(rp) == 1 and rp[0]["node_key"] is None and "kernel" in rp[0]["interpretation"]


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
    assert db.get_node_reasons(conn, plan_id=plan_id) == [] and conn.execute("SELECT COUNT(*) FROM change_reason").fetchone()[0] == 0
    assert "state graph unavailable" in (db.get_step(conn, review)["log_context"] or "")


def test_close_is_idempotent_for_reasons(conn, psg_with_plan, registry):
    plan_id, _ = _seed_registered_plan(conn, "P1", registry=registry)
    _close(conn, plan_id, registry)
    count = lambda: conn.execute("SELECT COUNT(*) FROM change_reason WHERE plan_id=?", (plan_id,)).fetchone()[0]
    n = count()
    api.review_and_complete(conn, plan_id, registry_path=registry)       # already terminal: no-op
    assert count() == n == 3


# ── DP phase 1 Task 4: three answer shapes, one tier each ────────────────────
from orchestrator import provenance as pv  # noqa: E402


def _utt(conn, text="please keep fiscal weeks for load_orders, finance reconciles weekly"):
    return pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P1", text=text, occurred_at="2026-09-15 10:00:00")


def test_fill_three_shapes_one_tier_each(conn, psg_with_plan):
    u = _utt(conn)
    ref = pv.insert_reference(conn, project="proj", kind="verbal", label="standup Tue", occurred_at="2026-09-15 09:00:00")
    r = reasons.fill(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg_with_plan, source="human", reasons=[
        {"node_key": "nk_a", "utterance_id": u, "span": [0, 24]},
        {"node_key": "nk_b", "interpretation": "finance wants weekly grain", "refs": [ref]},
        {"node_key": "nk_c", "unstated": True},
    ])
    assert r == {"filled": 2, "stated": 1, "asserted": 1, "unstated": 1, "unknown_keys": []}
    rows = {x["node_key"]: x for x in pv.reasons_for_plan(conn, "P1")}
    assert rows["nk_a"]["tier"] == "stated" and rows["nk_a"]["evidence_level"] == "verbal" and rows["nk_a"]["recorded_by"] == "human"
    assert rows["nk_b"]["tier"] == "asserted" and rows["nk_b"]["evidence_level"] == "verbal"       # a human interpretation is still asserted
    assert rows["nk_c"]["tier"] == "unstated" and rows["nk_c"]["interpretation"] is None
    text = conn.execute("SELECT text FROM utterance WHERE id=?", (u,)).fetchone()[0]
    assert text[rows["nk_a"]["verbatim_start"]:rows["nk_a"]["verbatim_end"]] == "please keep fiscal weeks"
    assert reasons.slots_for_plan(conn, "proj", "P1", psg_with_plan) == []
    assert reasons.unstated_ratio(conn, "proj", "P1") == {"slots": 3, "unstated": 1, "ratio": round(1 / 3, 4)}


def test_fill_refuses_the_old_text_shape_and_writes_nothing(conn, psg_with_plan):
    with pytest.raises(reasons.FillInputError, match='"text" is not accepted'):
        reasons.fill(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg_with_plan,
                     reasons=[{"node_key": "nk_a", "interpretation": "fine"}, {"node_key": "nk_b", "text": "weekly grain"}])
    assert conn.execute("SELECT COUNT(*) FROM change_reason").fetchone()[0] == 0
    with pytest.raises(reasons.FillInputError, match="exactly one"):
        reasons.fill(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg_with_plan,
                     reasons=[{"node_key": "nk_a", "interpretation": "x", "unstated": True}])
    with pytest.raises(reasons.FillInputError):
        reasons.fill(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg_with_plan, reasons=[{"node_key": "nk_a"}])
    u = _utt(conn)
    with pytest.raises(ValueError, match="outside"):
        reasons.fill(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg_with_plan,
                     reasons=[{"node_key": "nk_a", "utterance_id": u, "span": [0, 9999]}])
    assert conn.execute("SELECT COUNT(*) FROM change_reason").fetchone()[0] == 0


def test_draft_candidates_are_scored_and_previewed(conn, psg_with_plan):
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) VALUES "
                 "('P1', 'g', 'IN_PROGRESS', 'proj', 'declared', '2026-09-15 09:00:00')"); conn.commit()
    a = _utt(conn, "load_orders should keep paid orders only; the clean step drops nulls " + "x" * 200)
    b = _utt(conn, "unrelated chatter about the weather")
    c = _utt(conn, "load_orders load_orders load_orders — really, load orders")
    d = pv.insert_utterance(conn, session_id="s", project="other", plan_id=None, text="load_orders in another project",
                            occurred_at="2026-09-15 10:00:00")
    out = {x["node_key"]: x for x in reasons.draft(conn, "proj", "P1", psg_with_plan)}
    assert set(out) == {"nk_a", "nk_b", "nk_c"}
    cands = out["nk_a"]["candidates"]
    assert [x["utterance_id"] for x in cands] == [a, c] or [x["utterance_id"] for x in cands] == [c, a]
    assert all(x["score"] >= 1 and x["span"] == [0, len(conn.execute("SELECT text FROM utterance WHERE id=?", (x["utterance_id"],)).fetchone()[0])] for x in cands)
    assert len(next(x for x in cands if x["utterance_id"] == a)["preview"]) == 120
    assert b not in [x["utterance_id"] for x in cands] and d not in [x["utterance_id"] for x in cands]
    assert out["nk_b"]["candidates"] and out["nk_b"]["candidates"][0]["utterance_id"] == a          # "clean" matches nk_b


def test_close_mode_pending_backstops_unknown_without_asking(conn, psg_with_plan, registry, tmp_path):
    ext = tmp_path / "provledger-extensions.json"
    ext.write_text(json.dumps({"version": 1, "reasons": {"close_mode": "pending"}}))
    from orchestrator import extensions
    assert extensions.load(str(ext)).reasons_close_mode == "pending"
    assert extensions.load(None).reasons_close_mode == "ask"
    n = reasons.backstop_unstated(conn, project="proj", plan_id="P1", psg_db_path=psg_with_plan, commit=True, state="unknown")
    rows = pv.reasons_for_plan(conn, "P1")
    assert n == 3 and all(r["tier"] == "unstated" and r["state"] == "unknown" and r["recorded_by"] == "system" for r in rows)
    assert conn.execute("SELECT COUNT(*) FROM trigger_log WHERE plan_id='P1' AND verdict='ask'").fetchone()[0] == 0
