"""orchestrator.roots — a plan's root cause (task-level redesign, step 2).

A plan is a task; why it exists usually started earlier, in the user's words,
and several plans may carry the same root forward. At publish an agent says
"this starts a new root" (optionally pointing at the user's sentence) or "this
continues plan P"; saying nothing records `unknown`. Continuing a plan that
itself continued another resolves to where the root started, so every task
under one root points at the same plan."""
import pytest

from orchestrator import db, provenance as pv, roots


def _plan(conn, pid, project="proj", goal=None, user_query=None):
    db.insert_plan(conn, pid, goal or f"goal of {pid}", user_query=user_query, project=project, project_source="declared")
    conn.commit()


def _latest(conn, pid):
    return conn.execute("SELECT * FROM plan_root WHERE plan_id = ? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()


def test_no_root_is_recorded_as_unknown_by_the_system(conn):
    _plan(conn, "p1")
    out = roots.record(conn, plan_id="p1", root=None)
    row = _latest(conn, "p1")
    assert (row["kind"], row["root_plan_id"], row["recorded_by"], row["state"]) == ("unknown", None, "system", "asserted")
    assert out["kind"] == "unknown" and out["root_plan_id"] is None


def test_a_new_root_points_at_the_plan_itself_and_may_quote_the_user(conn):
    _plan(conn, "p1")
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="p1",
                            text="look at the last two months of orders", occurred_at="2026-09-15 10:00:00")
    roots.record(conn, plan_id="p1", root={"kind": "new", "utterance_id": u, "basis": "the lead set the window"})
    row = _latest(conn, "p1")
    assert (row["kind"], row["root_plan_id"], row["utterance_id"], row["recorded_by"]) == ("new", "p1", u, "agent")
    assert row["basis"] == "the lead set the window"


def test_continuing_resolves_to_where_the_root_started(conn):
    for p in ("p1", "p2", "p3"):
        _plan(conn, p)
    roots.record(conn, plan_id="p1", root={"kind": "new"})
    roots.record(conn, plan_id="p2", root={"kind": "continues", "plan_id": "p1", "basis": "holiday in the window"})
    out = roots.record(conn, plan_id="p3", root={"kind": "continues", "plan_id": "p2"})
    row = _latest(conn, "p3")
    assert (row["continues_plan_id"], row["root_plan_id"]) == ("p2", "p1") and out["root_plan_id"] == "p1"
    assert roots.root_of(conn, "p3") == "p1" and roots.root_of(conn, "p1") == "p1"


def test_continuing_a_plan_whose_root_is_unknown_starts_the_root_there(conn):
    _plan(conn, "p1")
    _plan(conn, "p2")
    roots.record(conn, plan_id="p1", root=None)
    roots.record(conn, plan_id="p2", root={"kind": "continues", "plan_id": "p1"})
    assert roots.root_of(conn, "p2") == "p1"


@pytest.mark.parametrize("root, says", [
    ({"kind": "continues", "plan_id": "nope"}, "no plan nope"),
    ({"kind": "continues"}, "plan_id"),
    ({"kind": "sideways"}, "kind"),
    ({"kind": "new", "utterance_id": 999}, "utterance 999"),
    ("new", "object"),
])
def test_a_root_that_cannot_be_recorded_is_refused_before_anything_is_written(conn, root, says):
    _plan(conn, "p1")
    with pytest.raises(ValueError, match=says):
        roots.validate(conn, root)
    with pytest.raises(ValueError, match=says):
        roots.record(conn, plan_id="p1", root=root)
    assert _latest(conn, "p1") is None


def test_a_new_root_cannot_quote_text_the_user_never_typed(conn):
    _plan(conn, "p1")
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="p1", occurred_at="2026-09-15 10:00:00",
                            text='<agent-message from="a1">\n[Subagent hand-back] look at two months\n</agent-message>')
    with pytest.raises(ValueError, match="not the user's words"):
        roots.validate(conn, {"kind": "new", "utterance_id": u})


def test_the_latest_row_counts_and_a_rejected_one_does_not(conn):
    for p in ("p1", "p2", "p3"):
        _plan(conn, p)
    roots.record(conn, plan_id="p1", root={"kind": "new"})
    roots.record(conn, plan_id="p2", root={"kind": "new"})
    roots.record(conn, plan_id="p3", root={"kind": "continues", "plan_id": "p1"})
    roots.record(conn, plan_id="p3", root={"kind": "continues", "plan_id": "p2"})
    assert roots.root_of(conn, "p3") == "p2"
    conn.execute("INSERT INTO plan_root (plan_id, kind, continues_plan_id, root_plan_id, state, recorded_by) "
                 "VALUES ('p3', 'continues', 'p2', 'p2', 'rejected', 'human')")
    assert roots.root_of(conn, "p3") is None


def test_recent_lists_the_projects_roots_newest_first_with_their_words_and_task_count(conn):
    _plan(conn, "a1", goal="tidy the feed", user_query="please tidy the feed")
    _plan(conn, "b1", goal="two-month window")
    _plan(conn, "b2")
    _plan(conn, "x1", project="other")
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="b1",
                            text="the lead wants the last two months as the POC", occurred_at="2026-09-15 10:00:00")
    roots.record(conn, plan_id="a1", root={"kind": "new"})
    roots.record(conn, plan_id="b1", root={"kind": "new", "utterance_id": u})
    roots.record(conn, plan_id="b2", root={"kind": "continues", "plan_id": "b1"})
    roots.record(conn, plan_id="x1", root={"kind": "new"})
    got = roots.recent(conn, "proj")
    assert [(r["root_plan_id"], r["tasks"]) for r in got] == [("b1", 2), ("a1", 1)]
    assert got[0]["words"] == "the lead wants the last two months as the POC"
    assert got[1]["words"] == "please tidy the feed"                      # no utterance: the plan's user_query


def test_tasks_under_a_root_lists_its_plans_except_the_one_asking(conn):
    for p in ("p1", "p2", "p3"):
        _plan(conn, p)
    conn.execute("UPDATE Plans SET status = 'COMPLETED' WHERE plan_id = 'p1'")
    roots.record(conn, plan_id="p1", root={"kind": "new"})
    roots.record(conn, plan_id="p2", root={"kind": "continues", "plan_id": "p1"})
    roots.record(conn, plan_id="p3", root={"kind": "continues", "plan_id": "p2"})
    got = roots.tasks_under(conn, "p1", exclude="p3")
    assert [(t["plan_id"], t["status"]) for t in got] == [("p1", "COMPLETED"), ("p2", "IN_PROGRESS")]
    assert got[0]["goal"] == "goal of p1" and got[0]["created_at"]
