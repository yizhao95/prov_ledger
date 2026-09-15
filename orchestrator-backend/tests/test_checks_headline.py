"""checks — two-layer pre-change check, the plan headline that never blocks, responses → influence (DP phase 2, Task 3)."""
import json
import sys
from pathlib import Path

import pytest

from orchestrator import constraints, context_pack as cp, db, provenance as pv

try:
    from orchestrator import checks
except ImportError:                       # RED: the module does not exist yet — every test below fails, pytest exits 1
    checks = None

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402


@pytest.fixture
def graph(tmp_path):
    """nk_a = pkg.m.load_orders reads `orders` and feeds pkg.m.clean; nk_u = pkg.m.parse was removed in run 2."""
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    for k, qn in (("nk_a", "pkg.m.load_orders"), ("nk_x", "pkg.m.clean"), ("nk_u", "pkg.m.parse")):
        ps.add_snapshot(c, 1, k, qn); ps.add_event(c, 1, len(k), "node_added", k)
    ps.add_run(c, 2, plan_id="P1")
    ps.add_snapshot(c, 2, "nk_a", "pkg.m.load_orders"); ps.add_snapshot(c, 2, "nk_x", "pkg.m.clean")
    ps.add_event(c, 2, 1, "node_removed", "nk_u", '{"qualified_name": "pkg.m.parse"}')
    c.executescript("""
        CREATE TABLE IF NOT EXISTS consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
        INSERT INTO node_type (id, name) VALUES (1, 'function');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (7, 1, 'load_orders', 'pkg.m.load_orders', 'pkg/m.py', 2, 'nk_a');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (8, 1, 'clean', 'pkg.m.clean', 'pkg/m.py', 2, 'nk_x');
        INSERT INTO consistency_card VALUES (7, '{"callers": ["pkg.m.main"], "callees": ["pkg.m.parse"], "output_consumers": ["pkg.m.clean"], "dtype_map": {}, "lineage_downstream": [], "reads": ["orders"]}');
        INSERT INTO consistency_card VALUES (8, '{"callers": [], "callees": [], "output_consumers": [], "dtype_map": {}, "lineage_downstream": []}');
    """)
    c.commit(); c.close()
    return str(path)


def _plan(conn, plan_id="P1"):
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source) VALUES (?, 'g', 'IN_PROGRESS', 'proj', 'declared')", (plan_id,))
    conn.commit()


def _pack(conn, graph, neighbors="constraints"):
    return cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, neighbors=neighbors, budget_tokens=100000, plan_id="P1")


def test_i1_zero_findings_still_gives_a_summary_and_a_row(conn, graph):
    _plan(conn)
    conn.execute("UPDATE analysis_run SET id=id") if False else None
    # a target with nothing on record and no removed upstream: the pack of an unknown node
    pack = cp.build(conn, project="proj", targets=["pkg.m.clean"], psg_db_path=graph, plan_id="P1", record=False)
    doc = checks.headline(conn, project="proj", plan_id="P1", pack=pack)
    assert doc["findings"] == [] and doc["summary"]["targets"] == 1 and doc["summary"]["layers"] == 2 and doc["summary"]["unanswered"] == 0
    assert conn.execute("SELECT COUNT(*) FROM headline WHERE plan_id='P1'").fetchone()[0] == 1
    assert json.loads(db.get_plan(conn, "P1")["headline_json"])["summary"]["findings"] == 0
    assert "0 findings" in checks.render(doc)


def test_layer_self_constraint_tier_follows_the_constraint_and_removed_upstream_is_observed(conn, graph):
    _plan(conn)
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0", text="never drop paid orders", occurred_at="2026-09-15 09:00:00")
    stated_c = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="organizational", role="constraint", verbatim=(u, 0, 22), recorded_by="human")
    asserted_c = constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="keep the region filter", rationale="legal")
    pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical", role="rejected_path", interpretation="tried dropping nulls first", rule_id="R6", recorded_by="system")
    eid = db.insert_expectation(conn, plan_id="P0", step_id=None, project="proj", target="pkg.m.load_orders", target_kind="node", claim="callers stay", channel="survival")
    db.insert_outcome(conn, expectation_id=eid, kind="survival", value={"signal": "changed_by", "plans": ["P0b"]}, source="survival", tier="derived", backfilled_by_plan="P0b")
    doc = checks.headline(conn, project="proj", plan_id="P1", pack=_pack(conn, graph))
    by_kind = {}
    for f in doc["findings"]:
        by_kind.setdefault(f["kind"], []).append(f)
    tiers = {f["evidence"]["reason_id"]: f["tier"] for f in by_kind["active_constraint"]}
    assert tiers == {stated_c: "stated", asserted_c: "asserted"} and all(f["severity"] == "blocking" and f["layer"] == "self" for f in by_kind["active_constraint"])
    assert by_kind["rejected_path"][0]["tier"] == "derived" and by_kind["rejected_path"][0]["severity"] == "warning"
    assert by_kind["prior_outcome_failed"][0]["evidence"] == {"expectation_id": eid} and by_kind["prior_outcome_failed"][0]["tier"] == "derived"
    ru = by_kind["removed_upstream"][0]
    assert ru["tier"] == "observed" and ru["severity"] == "blocking" and "pkg.m.parse" in ru["text"] and ru["evidence"]["event_id"]
    assert by_kind["downstream_break"][0]["layer"] == "impact" and "pkg.m.clean" in by_kind["downstream_break"][0]["text"]
    assert by_kind["unverified_upstream"][0]["severity"] == "info"
    assert doc["summary"]["blocking"] == 3 and doc["summary"]["unanswered"] == 3
    text = checks.render(doc)
    assert "plan headline · 1 targets · 2 layers" in text and "3 findings unanswered" in text and "⚠ blocking" in text


def test_unanswered_does_not_block_but_a_human_block_true_constraint_is_hard(conn, graph):
    _plan(conn)
    constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="never read raw prod", rationale="x")
    doc = checks.headline(conn, project="proj", plan_id="P1", pack=_pack(conn, graph))
    ac = next(f for f in doc["findings"] if f["kind"] == "active_constraint")
    assert doc["summary"]["unanswered"] == 2 and doc["summary"]["hard_unanswered"] == 0 and not ac["hard"]   # + the removed upstream, also blocking
    doc2 = checks.headline(conn, project="proj", plan_id="P1", pack=_pack(conn, graph), hard_statements=frozenset({"never read raw prod"}))
    assert doc2["summary"]["hard_unanswered"] == 1 and next(f for f in doc2["findings"] if f["kind"] == "active_constraint")["hard"] is True
    assert "[block]" in checks.render(doc2)
    # a rule-derived constraint row (recorded_by system) is never hard, whatever the extensions say
    pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="organizational", role="constraint", interpretation="sys rule", rule_id="constraint_bypassed", recorded_by="system")
    doc3 = checks.headline(conn, project="proj", plan_id="P1", pack=_pack(conn, graph), hard_statements=frozenset({"sys rule"}))
    assert doc3["summary"]["hard_unanswered"] == 0


def test_respond_writes_influence_with_the_right_by_and_refuses_a_second_answer(conn, graph):
    _plan(conn)
    cid = constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="keep paid only", rationale="finance")
    other = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_x", kind="technical", interpretation="clean drops nulls", recorded_by="agent")
    doc = checks.headline(conn, project="proj", plan_id="P1", pack=_pack(conn, graph))
    fid = next(f["id"] for f in doc["findings"] if f["kind"] == "active_constraint")
    rid = checks.respond(conn, plan_id="P1", finding_id=fid, action="proceed", rationale="the filter moves to clean", by="agent", cites=[other])
    rows = [tuple(r) for r in conn.execute("SELECT reason_id, node_key, via, by FROM influence ORDER BY reason_id")]
    assert rows == [(cid, "pkg.m.load_orders", "headline_response", "agent"), (other, "pkg.m.load_orders", "headline_response", "agent")]
    latest = checks.latest(conn, plan_id="P1")
    f = next(f for f in latest["findings"] if f["id"] == fid)
    assert f["response"] == {"action": "proceed", "rationale": "the filter moves to clean", "by": "agent", "cites": [other]}
    assert latest["summary"]["unanswered"] == 1 and latest["summary"]["adopted"] == 2 and "→ proceed (agent)" in checks.render(latest)   # removed_upstream still open
    with pytest.raises(Exception):                       # UNIQUE (headline_id, finding_id): changing the answer is a new headline
        checks.respond(conn, plan_id="P1", finding_id=fid, action="revise", rationale=None, by="human")
    with pytest.raises(ValueError, match="does not exist"):                  # a cited record must exist (I3), checked before anything is written
        checks.respond(conn, plan_id="P1", finding_id=fid, action="proceed", rationale=None, by="human", cites=[9999])
    with pytest.raises(ValueError, match="no finding"):
        checks.respond(conn, plan_id="P1", finding_id="nope:1", action="proceed", rationale=None, by="human")
    checks.headline(conn, project="proj", plan_id="P1", pack=_pack(conn, graph))                   # recomputed: a new row
    checks.respond(conn, plan_id="P1", finding_id=fid, action="revise", rationale="ok", by="human")
    assert conn.execute("SELECT by FROM influence ORDER BY id DESC LIMIT 1").fetchone()[0] == "human"


def test_asserted_notes_must_cite_existing_reasons(conn, graph):
    _plan(conn)
    other = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_x", kind="technical", interpretation="x", recorded_by="agent")
    doc = checks.headline(conn, project="proj", plan_id="P1", pack=_pack(conn, graph),
                          notes=[{"finding_kind": "similar_intent", "cites": [other], "text": "same shape as P0's rejected path"}])
    f = next(f for f in doc["findings"] if f["kind"] == "similar_intent")
    assert f["tier"] == "asserted" and f["severity"] == "warning" and f["evidence"] == {"reason_ids": [other]}
    with pytest.raises(ValueError, match="does not exist"):
        checks.headline(conn, project="proj", plan_id="P1", pack=_pack(conn, graph), notes=[{"finding_kind": "similar_intent", "cites": [9999], "text": "x"}])
    with pytest.raises(ValueError, match="cite"):
        checks.headline(conn, project="proj", plan_id="P1", pack=_pack(conn, graph), notes=[{"finding_kind": "similar_intent", "text": "x"}])


def test_close_headline_turns_proceeded_and_unanswered_blocking_findings_into_expectations(conn, graph):
    _plan(conn)
    constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="keep paid only", rationale="f")
    constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="keep region", rationale="l")
    doc = checks.headline(conn, project="proj", plan_id="P1", pack=_pack(conn, graph))
    ids = [f["id"] for f in doc["findings"] if f["kind"] == "active_constraint"]
    checks.respond(conn, plan_id="P1", finding_id=ids[0], action="proceed", rationale="moves downstream", by="agent")
    out = checks.close_headline(conn, plan_id="P1", commit=True)
    assert out == {"expectations": 3, "unanswered": 2, "proceeded": 1}          # 2 constraints (1 proceeded, 1 unanswered) + the removed upstream
    claims = [r[0] for r in conn.execute("SELECT claim FROM expectations WHERE plan_id='P1' ORDER BY id")]
    assert any(c.startswith("越过 #") for c in claims) and any(c.startswith("未回答 #") for c in claims)
    assert all("后本 plan 内无 step 失败" in c for c in claims)
    assert checks.close_headline(conn, plan_id="P1", commit=True)["expectations"] == 0            # idempotent


def test_i11_showing_is_not_adopting(conn, graph):
    _plan(conn)
    constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="keep paid only", rationale="f")
    checks.headline(conn, project="proj", plan_id="P1", pack=_pack(conn, graph))
    assert conn.execute("SELECT COUNT(*) FROM read_hit WHERE plan_id='P1'").fetchone()[0] >= 1
    assert conn.execute("SELECT COUNT(*) FROM influence").fetchone()[0] == 0
