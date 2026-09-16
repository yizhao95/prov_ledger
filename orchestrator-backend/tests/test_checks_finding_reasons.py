"""DP phase 2d (Task 0): a finding carries the words recorded at the time.

Before this, `removed_upstream` said only "upstream pkg.m.parse was removed in
run 2" — true, and useless: the agent reading the headline cannot see WHY it was
removed, so it cannot adopt that record either. Every such finding now appends
the reason recorded against that node for that run (verbatim first), names the
plan it was recorded in, and carries its reason_id so answering the finding can
cite it. A node with no reason reads 当时未说明 — never silence.
"""
import sys
from pathlib import Path

import pytest

from orchestrator import checks, context_pack as cp, provenance as pv

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

VERBATIM = "上游 parse 收到通知要下线，先别再依赖它"


@pytest.fixture
def graph(tmp_path):
    """nk_a = pkg.m.load_orders calls pkg.m.parse (nk_u), which was removed in run 2."""
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    for k, qn in (("nk_a", "pkg.m.load_orders"), ("nk_x", "pkg.m.clean"), ("nk_u", "pkg.m.parse")):
        ps.add_snapshot(c, 1, k, qn); ps.add_event(c, 1, len(k), "node_added", k)
    ps.add_run(c, 2, plan_id="P0")
    ps.add_snapshot(c, 2, "nk_a", "pkg.m.load_orders"); ps.add_snapshot(c, 2, "nk_x", "pkg.m.clean")
    ps.add_event(c, 2, 1, "node_removed", "nk_u", '{"qualified_name": "pkg.m.parse"}')
    c.executescript("""
        CREATE TABLE IF NOT EXISTS consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
        INSERT INTO node_type (id, name) VALUES (1, 'function');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (7, 1, 'load_orders', 'pkg.m.load_orders', 'pkg/m.py', 2, 'nk_a');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (8, 1, 'clean', 'pkg.m.clean', 'pkg/m.py', 2, 'nk_x');
        INSERT INTO consistency_card VALUES (7, '{"callers": [], "callees": ["pkg.m.parse"], "output_consumers": [], "dtype_map": {}, "lineage_downstream": []}');
        INSERT INTO consistency_card VALUES (8, '{"callers": [], "callees": [], "output_consumers": [], "dtype_map": {}, "lineage_downstream": []}');
    """)
    c.commit(); c.close()
    return str(path)


def _plans(conn):
    for pid in ("P0", "P1"):
        conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source) VALUES (?, 'g', 'IN_PROGRESS', 'proj', 'declared')", (pid,))
    conn.commit()


def _pack(conn, graph):
    return cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph,
                    budget_tokens=100000, plan_id="P1", record=False)


def _finding(findings, kind):
    return next(f for f in findings if f.kind == kind)


def test_removed_upstream_quotes_the_users_words_and_names_the_plan(conn, graph):
    _plans(conn)
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0",
                            text=VERBATIM, occurred_at="2026-09-15 10:00:00")
    rid = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_u", kind="technical", run_id=2,
                           verbatim=(u, 0, len(VERBATIM)), recorded_by="agent")
    f = _finding(checks.layer_self(_pack(conn, graph), conn=conn), "removed_upstream")
    assert VERBATIM in f.text and "因为" in f.text and "用户原话" in f.text and "P0" in f.text
    assert f.evidence["reason_id"] == rid and f.evidence["plan_id"] == "P0"
    assert f.evidence["event_id"]                                  # the observation is still there


def test_a_removal_with_nothing_recorded_says_so_instead_of_staying_quiet(conn, graph):
    _plans(conn)
    f = _finding(checks.layer_self(_pack(conn, graph), conn=conn), "removed_upstream")
    assert "当时未说明" in f.text and "reason_id" not in f.evidence


def test_an_agents_reading_is_not_labelled_as_the_users_words(conn, graph):
    _plans(conn)
    rid = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_u", kind="technical", run_id=2,
                           interpretation="parse 的调用点全部迁到 read_orders 了", recorded_by="agent")
    f = _finding(checks.layer_self(_pack(conn, graph), conn=conn), "removed_upstream")
    assert "parse 的调用点全部迁到 read_orders 了" in f.text and "用户原话" not in f.text and "agent" in f.text
    assert f.evidence["reason_id"] == rid


def test_prior_outcome_failed_carries_the_reason_recorded_on_the_target(conn, graph):
    _plans(conn)
    conn.execute("INSERT INTO expectations (project, plan_id, target, target_kind, claim, channel, created_at) "
                 "VALUES ('proj', 'P0', 'pkg.m.load_orders', 'node', 'still read by clean', 'survival', '2026-09-15 10:00:00')")
    eid = conn.execute("SELECT id FROM expectations WHERE plan_id='P0'").fetchone()[0]
    conn.execute("INSERT INTO outcomes (expectation_id, kind, value_json, source, tier, observed_at) "
                 "VALUES (?, 'survival', '{\"signal\": \"removed\"}', 'analyzer', 'observed', '2026-09-15 12:00:00')", (eid,))
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0",
                            text=VERBATIM, occurred_at="2026-09-15 10:00:00")
    rid = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical",
                           verbatim=(u, 0, len(VERBATIM)), recorded_by="agent")
    conn.commit()
    f = _finding(checks.layer_self(_pack(conn, graph), conn=conn), "prior_outcome_failed")
    assert VERBATIM in f.text and "用户原话" in f.text
    assert f.evidence["reason_id"] == rid and f.evidence["expectation_id"] == eid


def test_layer_self_without_a_connection_still_works_and_says_nothing_it_cannot_know(conn, graph):
    """The reader is optional: a caller with no DB gets the 2b text, not a crash
    and not an invented reason."""
    _plans(conn)
    f = _finding(checks.layer_self(_pack(conn, graph)), "removed_upstream")
    assert "was removed in run 2" in f.text and "因为" not in f.text and "当时未说明" not in f.text


# ── the human path: `provledger note` records no run_id (DP 2d, Task 5) ──────
# The scenario this whole feature exists for is a PERSON explaining a removal in
# their own words. `provledger note --node <qn>` is the documented way to record
# that, and it writes no run_id — so a lookup keyed only on run/plan would print
# 当时未说明 for exactly the case that matters. The fallback is the node itself.

def test_a_note_with_no_run_id_is_still_the_reason_for_the_removal(conn, graph):
    _plans(conn)
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0",
                            text=VERBATIM, occurred_at="2026-09-15 10:00:00")
    rid = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_u", kind="technical",
                           verbatim=(u, 0, len(VERBATIM)), recorded_by="human")   # no run_id, as note does
    f = _finding(checks.layer_self(_pack(conn, graph), conn=conn), "removed_upstream")
    assert VERBATIM in f.text and "用户原话" in f.text and "P0" in f.text
    assert f.evidence["reason_id"] == rid


def test_a_reason_tied_to_the_run_still_wins_over_a_loose_one(conn, graph):
    _plans(conn)
    pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_u", kind="technical",
                     interpretation="一条没有 run 的旧记录", recorded_by="human")
    tied = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_u", kind="technical", run_id=2,
                            interpretation="就是这次 run 记的", recorded_by="agent")
    f = _finding(checks.layer_self(_pack(conn, graph), conn=conn), "removed_upstream")
    assert "就是这次 run 记的" in f.text and f.evidence["reason_id"] == tied


def test_a_reason_on_a_different_node_is_never_borrowed(conn, graph):
    _plans(conn)
    pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_x", kind="technical",
                     interpretation="这是别的节点的理由", recorded_by="agent")
    f = _finding(checks.layer_self(_pack(conn, graph), conn=conn), "removed_upstream")
    assert "这是别的节点的理由" not in f.text and "当时未说明" in f.text
