"""A task reason corrected by a later one (`supersedes` on a reason-fill task).

The close writes the task's reason before the evidence pass looks for where the
cause was set. When the pass finds that mail or meeting, the agent writes its
reading of the source as a new task reason that supersedes the old row; when the
user later says why, their words supersede it as stated. The old row keeps its
words and points at its successor, and readers show the successor (FL-238).
Only the user's words may replace the user's words."""
import sys
from pathlib import Path

import pytest

from orchestrator import db, provenance as pv, reasons
from orchestrator.reasons import FillInputError

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

PLAN = "P1"
NODES = {"nk_a": "pkg.m.load_orders", "nk_b": "pkg.m.clean", "nk_c": "pkg.m.weekly_totals"}
AT = "2026-09-14 16:00:00"


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
    db.insert_plan(conn, PLAN, "drop the region from the rollup", user_query="update the weekly rollup",
                   project="proj", project_source="declared")
    db.insert_plan(conn, "P2", "another plan", project="proj", project_source="declared")
    conn.commit()
    return PLAN


def _fill(conn, graph, tasks, bundle=None, plan_id=PLAN):
    return reasons.fill_tasks(conn, project="proj", plan_id=plan_id, run_id=2, tasks=tasks, bundle=bundle,
                              psg_db_path=graph)


def _mail(conn, uri="https://mail.example.com/m/1"):
    return pv.insert_reference(conn, project="proj", kind="email", label="Q3 scope · the finance lead",
                               occurred_at=AT, uri=uri, said_by="the finance lead",
                               excerpt="leave the region out of the Q3 rollup until the audit closes")


def _row(conn, rid):
    return conn.execute("SELECT * FROM change_reason WHERE id = ?", (rid,)).fetchone()


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM change_reason").fetchone()[0]


def test_a_reading_of_a_found_source_supersedes_the_unstated_task(conn, graph, plan):
    (old,) = _fill(conn, graph, [{"unstated": True}])["reason_ids"]
    ref = _mail(conn)
    out = _fill(conn, graph, [{"supersedes": old, "refs": [ref],
                               "interpretation": "the finance lead asked to leave the region out until the audit closes"}])
    (new,) = out["reason_ids"]
    assert out["superseded"] == [old] and out["asserted"] == 1
    assert _row(conn, old)["superseded_by"] == new
    assert _row(conn, old)["tier"] == "unstated"          # the old row keeps what it was
    assert [r["id"] for r in reasons._live_task_reasons(conn, PLAN)] == [new]
    assert pv.get_reason(conn, new)["tier"] == "asserted"
    assert pv.get_reason(conn, new)["evidence_level"] == "linked"
    for k in NODES:
        assert reasons.covering_task_reasons(conn, k, [PLAN]) == [new]


def test_the_users_later_words_supersede_a_reading(conn, graph, plan):
    (old,) = _fill(conn, graph, [{"interpretation": "the plan says to drop the region"}])["reason_ids"]
    said = "drop it, the audit is still open"
    u = pv.insert_utterance(conn, session_id="note", project="proj", plan_id=PLAN, text=said, occurred_at=AT)
    (new,) = _fill(conn, graph, [{"supersedes": old, "utterance_id": u, "span": [0, len(said)]}])["reason_ids"]
    assert pv.get_reason(conn, new)["tier"] == "stated"
    assert _row(conn, old)["superseded_by"] == new


def test_the_pointers_on_the_old_row_come_with_their_stance(conn, graph, plan):
    (old,) = _fill(conn, graph, [{"unstated": True}])["reason_ids"]
    found, against = _mail(conn), _mail(conn, uri="https://mail.example.com/m/2")
    pv.link_reference(conn, old, found)
    pv.link_reference(conn, old, against, stance="contradicts")
    (new,) = _fill(conn, graph, [{"supersedes": old, "interpretation": "the audit is why"}])["reason_ids"]
    links = conn.execute("SELECT reference_id, stance FROM reference_link WHERE reason_id = ? ORDER BY reference_id",
                         (new,)).fetchall()
    assert [tuple(r) for r in links] == [(found, "supports"), (against, "contradicts")]
    assert conn.execute("SELECT COUNT(*) FROM reference_link WHERE reason_id = ?", (old,)).fetchone()[0] == 2


def test_a_correction_keeps_the_task_its_nodes_and_its_kind(conn, graph, plan):
    first, second = _fill(conn, graph, [{"interpretation": "the rollup", "kind": "organizational"},
                                        {"unstated": True, "nodes": ["pkg.m.clean"]}],
                          bundle="one review")["reason_ids"]
    (new,) = _fill(conn, graph, [{"supersedes": second, "interpretation": "the cleaning rule came from the mail"}])["reason_ids"]
    assert reasons.covering_task_reasons(conn, "nk_b", [PLAN]) == [new]
    assert reasons.covering_task_reasons(conn, "nk_a", [PLAN]) == [first]
    (fixed,) = _fill(conn, graph, [{"supersedes": first, "interpretation": "the rollup, as the finance lead asked"}])["reason_ids"]
    assert _row(conn, fixed)["kind"] == "organizational"


def test_two_corrections_in_one_call_need_no_bundle(conn, graph, plan):
    a, b = _fill(conn, graph, [{"unstated": True}, {"unstated": True, "nodes": ["nk_c"]}], bundle="one review")["reason_ids"]
    out = _fill(conn, graph, [{"supersedes": a, "interpretation": "x"}, {"supersedes": b, "interpretation": "y"}])
    assert out["superseded"] == [a, b] and out["tasks"] == 2


@pytest.mark.parametrize("make_bad, says", [
    (lambda conn, graph, old: [{"supersedes": old, "interpretation": "my reading"}], "user's words"),
    (lambda conn, graph, old: [{"supersedes": old, "unstated": True}], "user's words"),
    (lambda conn, graph, old: [{"supersedes": 999999, "interpretation": "x"}], "999999"),
    (lambda conn, graph, old: [{"supersedes": old, "interpretation": "x"}, {"supersedes": old, "interpretation": "y"}], "twice"),
    (lambda conn, graph, old: [{"supersedes": old, "utterance_id": 1, "span": [0, 4], "nodes": ["nk_a"]}], "nodes"),
    (lambda conn, graph, old: [{"supersedes": old, "utterance_id": 1, "span": [0, 4]}, {"unstated": True}], "correction"),
    (lambda conn, graph, old: [{"supersedes": "7", "interpretation": "x"}], "supersedes"),
])
def test_a_stated_task_is_replaced_only_by_the_users_words_and_bad_ids_write_nothing(conn, graph, plan, make_bad, says):
    said = "drop the region"
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id=PLAN, text=said, occurred_at=AT)
    assert u == 1
    (old,) = _fill(conn, graph, [{"utterance_id": u, "span": [0, len(said)]}])["reason_ids"]
    before = _count(conn)
    with pytest.raises(FillInputError, match=says):
        _fill(conn, graph, make_bad(conn, graph, old), bundle="b")
    assert _count(conn) == before and _row(conn, old)["superseded_by"] is None


def test_only_a_live_task_reason_of_this_plan_can_be_superseded(conn, graph, plan):
    (other,) = _fill(conn, graph, [{"unstated": True}], plan_id="P2")["reason_ids"]
    node_reason = pv.insert_reason(conn, project="proj", plan_id=PLAN, node_key="nk_a", kind="technical",
                                   interpretation="a person's note on one node")
    (old,) = _fill(conn, graph, [{"unstated": True}])["reason_ids"]
    _fill(conn, graph, [{"supersedes": old, "interpretation": "first correction"}])
    for bad, says in ((other, "P2"), (node_reason, "task reason"), (old, "already")):
        before = _count(conn)
        with pytest.raises(FillInputError, match=says):
            _fill(conn, graph, [{"supersedes": bad, "interpretation": "x"}])
        assert _count(conn) == before
