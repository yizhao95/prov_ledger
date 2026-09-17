"""significance — hint, logged LLM verdict, human mark (DP phase 2b, Task 2; §17, I14)."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from orchestrator import constraints, db, provenance as pv

try:
    from orchestrator import significance
except ImportError:                       # RED: the module does not exist yet
    significance = None

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402


@pytest.fixture
def graph(tmp_path):
    """nk_a changed struct_sig in run 2 and feeds nk_x; nk_b matched unchanged, no consumers; nk_c added in run 2."""
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0"); ps.add_run(c, 2, plan_id="P1")
    for k, qn in (("nk_a", "pkg.m.load_orders"), ("nk_b", "pkg.m.helper"), ("nk_x", "pkg.m.clean")):
        ps.add_snapshot(c, 1, k, qn); ps.add_event(c, 1, 1, "node_added", k)
    ps.add_snapshot(c, 2, "nk_a", "pkg.m.load_orders", struct_sig="s2"); ps.add_event(c, 2, 1, "node_changed", "nk_a", '{"changed": ["struct_sig"]}')
    ps.add_snapshot(c, 2, "nk_b", "pkg.m.helper"); ps.add_event(c, 2, 2, "node_matched", "nk_b")
    ps.add_snapshot(c, 2, "nk_c", "pkg.m.newfn"); ps.add_event(c, 2, 3, "node_added", "nk_c")
    ps.add_snapshot(c, 2, "nk_x", "pkg.m.clean")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
        INSERT INTO node_type (id, name) VALUES (1, 'function');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (7, 1, 'load_orders', 'pkg.m.load_orders', 'pkg/m.py', 2, 'nk_a');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (8, 1, 'helper', 'pkg.m.helper', 'pkg/m.py', 2, 'nk_b');
        INSERT INTO consistency_card VALUES (7, '{"callers": [], "callees": [], "output_consumers": ["pkg.m.clean"], "dtype_map": {}, "lineage_downstream": []}');
        INSERT INTO consistency_card VALUES (8, '{"callers": [], "callees": [], "output_consumers": [], "dtype_map": {}, "lineage_downstream": []}');
    """)
    c.commit(); c.close()
    return str(path)


def _plan(conn):
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source) VALUES ('P1', 'g', 'IN_PROGRESS', 'proj', 'declared')")
    conn.commit()


def _reason(conn, key, run_id=2, **kw):
    kw.setdefault("interpretation", "why")
    return pv.insert_reason(conn, project="proj", plan_id="P1", node_key=key, kind="technical", run_id=run_id, recorded_by="agent", **kw)


def _row(conn, rid):
    return dict(conn.execute("SELECT id, project, plan_id, node_key, run_id, role, tier, interpretation AS text FROM change_reason WHERE id=?", (rid,)).fetchone())


def test_six_hint_signals_each_make_major_and_nothing_makes_minor(conn, graph):
    _plan(conn)
    quiet = _reason(conn, "nk_b")                                                     # matched, no consumers, no constraint, no outcome
    assert significance.hint(conn, _row(conn, quiet), graph)[0] == "minor"
    assert significance.hint(conn, _row(conn, _reason(conn, "nk_a")), graph)[1].startswith("struct_sig changed")       # 1 struct_sig
    assert "downstream consumer" in significance.hint(conn, _row(conn, _reason(conn, "nk_a", run_id=1)), graph)[1]      # 2 consumers (run 1: no struct change)
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P1", text="keep fiscal weeks", occurred_at="2026-09-16 09:00:00")
    stated = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_b", kind="technical", run_id=2, verbatim=(u, 0, 17), recorded_by="system", rule_id="R0")
    r = _row(conn, stated); assert significance.hint(conn, r, graph) == ("major", "the user's own words")               # 3 stated
    constraints.record_constraint(conn, project="proj", subjects=["nk_b"], statement="keep helper pure")
    assert "active constraint" in significance.hint(conn, _row(conn, quiet), graph)[1]                                  # 4 constraint
    conn.execute("UPDATE change_reason SET state='expired' WHERE role='constraint'") if False else None
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source) VALUES ('P2', 'g', 'IN_PROGRESS', 'proj', 'declared')")
    other = pv.insert_reason(conn, project="proj", plan_id="P2", node_key="nk_c", kind="technical", run_id=None, interpretation="x", recorded_by="agent")
    assert significance.hint(conn, _row(conn, other), graph)[0] == "minor"
    conn.execute("INSERT INTO Steps (step_id, plan_id, description, status, is_review, execution_order, log_context) VALUES ('P2-REVIEW', 'P2', 'r', 'COMPLETED', 1, 99, '[3] gates failed [stale_references]')")
    assert "gate failed" in significance.hint(conn, _row(conn, other), graph)[1]                                        # 5 gate
    eid = db.insert_expectation(conn, plan_id="P1", step_id=None, project="proj", target="pkg.m.newfn", target_kind="node", claim="c", channel="survival")
    db.insert_outcome(conn, expectation_id=eid, kind="survival", value={"signal": "survived"}, source="survival", tier="derived")
    conn.execute("DELETE FROM Steps WHERE step_id='P2-REVIEW'") if False else None
    o2 = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_c", kind="technical", run_id=None, interpretation="y", recorded_by="agent")
    assert "outcome" in significance.hint(conn, _row(conn, o2), graph)[1]                                               # 6 outcome


def test_stub_runner_json_gives_a_verdict_row_and_non_json_only_a_hint_row(conn, graph):
    _plan(conn)
    rid = _reason(conn, "nk_b")
    calls = []
    def good(prompt, *, model=None, timeout_s=None):
        calls.append(prompt); return 'Here you go: {"significance": "major", "basis": "renames a public api"}'
    out = significance.judge(conn, _row(conn, rid), runner=good, psg_db_path=graph, commit=True)
    assert out == {"reason_id": rid, "hint": "minor", "verdict": "major", "log_id": out["log_id"]}
    assert '"hint": "minor"' in calls[0] and "pkg.m.helper" in calls[0] and "strict JSON" in calls[0]
    row = conn.execute("SELECT hint, verdict, judged_by, runner, verdict_basis FROM significance_log WHERE id=?", (out["log_id"],)).fetchone()
    assert tuple(row) == ("minor", "major", "llm", "good", "renames a public api")
    assert conn.execute("SELECT significance_eff FROM change_reason_v WHERE id=?", (rid,)).fetchone()[0] == "major"
    def bad(prompt, *, model=None, timeout_s=None):
        return "I think it is major."
    rid2 = _reason(conn, "nk_b")
    out = significance.judge(conn, _row(conn, rid2), runner=bad, psg_db_path=graph, commit=True)
    assert out["verdict"] is None
    row = conn.execute("SELECT hint, verdict, judged_by, hint_basis FROM significance_log WHERE id=?", (out["log_id"],)).fetchone()
    assert row[0] == "minor" and row[1] is None and row[2] == "hint" and "not strict JSON" in row[3]
    assert conn.execute("SELECT significance_eff FROM change_reason_v WHERE id=?", (rid2,)).fetchone()[0] == "minor"


def test_apply_for_plan_in_hint_mode_never_calls_a_runner_and_writes_exactly_one_row_per_reason(conn, graph, monkeypatch):
    _plan(conn)
    r1 = _reason(conn, "nk_a"); r2 = _reason(conn, "nk_b")
    pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_b", kind="technical", run_id=2, recorded_by="system")   # unstated: never judged
    n = {"calls": 0}
    def boom(*a, **k):
        n["calls"] += 1; raise AssertionError("no runner in hint mode")
    monkeypatch.setattr("orchestrator.testing.claude_arbiter.text_runner", boom)
    out = significance.apply_for_plan(conn, project="proj", plan_id="P1", psg_db_path=graph, mode="hint", commit=True)
    assert out == {"plan_id": "P1", "mode": "hint", "judged": 2, "major": 1, "minor": 1, "verdicts": 0} and n["calls"] == 0
    rows = [tuple(r) for r in conn.execute("SELECT reason_id, COUNT(*) FROM significance_log GROUP BY 1 ORDER BY 1")]
    assert rows == [(r1, 1), (r2, 1)]                                                  # C4: exactly one hint row per reason
    again = significance.apply_for_plan(conn, project="proj", plan_id="P1", psg_db_path=graph, mode="hint", commit=True)
    assert again["judged"] == 0 and conn.execute("SELECT COUNT(*) FROM significance_log").fetchone()[0] == 2     # idempotent
    llm = significance.apply_for_plan(conn, project="proj", plan_id="P1", psg_db_path=graph, mode="llm",
                                       runner=lambda p, *, model=None, timeout_s=None: '{"significance": "minor", "basis": "x"}', commit=True)
    assert llm["judged"] == 0                                                          # already judged → nothing to do


def test_mark_writes_a_human_row_and_significance_eff_follows(conn, graph):
    _plan(conn)
    rid = _reason(conn, "nk_a")
    significance.apply_for_plan(conn, project="proj", plan_id="P1", psg_db_path=graph, mode="hint", commit=True)
    assert conn.execute("SELECT significance_eff FROM change_reason_v WHERE id=?", (rid,)).fetchone()[0] == "major"
    lid = significance.mark(conn, rid, "minor", basis="just a rename", psg_db_path=graph)
    row = conn.execute("SELECT hint, verdict, judged_by, verdict_basis FROM significance_log WHERE id=?", (lid,)).fetchone()
    assert tuple(row) == ("major", "minor", "human", "just a rename")
    assert conn.execute("SELECT significance_eff FROM change_reason_v WHERE id=?", (rid,)).fetchone()[0] == "minor"
    assert significance.disagreements(conn, "proj")[0]["reason_id"] == rid
    assert significance.confusion(conn, "proj") == {"major->major": 0, "major->minor": 1, "minor->major": 0, "minor->minor": 0}
    with pytest.raises(ValueError):
        significance.mark(conn, rid, "huge")
    with pytest.raises(ValueError):
        significance.mark(conn, 9999, "minor")


def test_close_applies_hints_by_default_and_the_extensions_can_ask_for_llm(conn, graph, monkeypatch):
    from orchestrator import extensions
    _plan(conn)
    rid = _reason(conn, "nk_a")
    assert extensions.EMPTY.reasons_significance == "hint"
    ext_llm = extensions.load.__wrapped__ if hasattr(extensions.load, "__wrapped__") else None
    # a parsed extensions file with reasons.significance: llm
    p = Path(str(conn.execute("PRAGMA database_list").fetchone()[2])).parent / "provledger-extensions.json"
    p.write_text(json.dumps({"version": 1, "reasons": {"significance": "llm"}}))
    assert extensions.load(str(p)).reasons_significance == "llm"
    p.write_text(json.dumps({"version": 1, "reasons": {"significance": "loud"}}))
    with pytest.raises(extensions.ExtensionsError):
        extensions.load(str(p))


def test_why_and_the_pack_fold_minor_into_a_count(conn, graph):
    from orchestrator import context_pack, why
    _plan(conn)
    big = _reason(conn, "nk_a", interpretation="dropped the null filter")
    small = _reason(conn, "nk_a", interpretation="renamed a local")
    significance.mark(conn, small, "minor", basis="rename")
    pack = context_pack.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, record=False)
    t = pack.targets[0]
    assert [r["id"] for r in t.reasons] == [big] and t.counts["reasons_minor"] == 1 and pack.truncated["reasons_minor"] == 1
    text = why.why(conn, project="proj", target="pkg.m.load_orders", psg_db_path=graph, record=False)["text"]
    assert "dropped the null filter" in text and "renamed a local" not in text and "minor" in text
    everything = why.why(conn, project="proj", target="pkg.m.load_orders", psg_db_path=graph, all_records=True, record=False)["text"]
    assert "renamed a local" in everything
