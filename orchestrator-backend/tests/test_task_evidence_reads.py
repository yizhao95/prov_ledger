"""What backs a task reason is read with it, and old per-node records say what
they are (task-level redesign, step 3b).

A task's source usually hangs on the root's words — the user's sentence that
set the cause — or on the root plan's task reason, not on the row a reader
reaches from a node. The reads a person runs (the /ledger fact table, the
/receipts timeline) list every source that reaches the task reason, with who
said it and the passage they said, through evidence.sources_for_reason, the one
read of what backs a record.

The close wrote one reason per changed node before 0.4.8 (rules R0-R5, the
agent's per-node answers, the per-node unstated backstop). Those rows stay and
read as before, marked as per-node records from before task reasons, so a
reader does not take a sentence about one function for the task's reason. A
person's own note on a node, and the external trigger's row, are not that.
"""
import sys
from pathlib import Path

import pytest

from orchestrator import db, external_trigger, provenance as pv, reasons, roots, why
from orchestrator.ask import facts, receipts

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

SAID = "The v2 orders feed stops carrying orders.discount from the June release."
TASK = "estimate the discount rate from list_price, since the feed no longer carries it"
EXCERPT = "From the June release the v2 feed no longer carries orders.discount; consumers must stop reading it."
OLD = "discount_rate reads list_price now"
MARK = "per-node record, before task reasons"
NODES = {"nk_a": "pkg.m.discount_rate", "nk_d": "pkg.m.export"}


@pytest.fixture
def world(conn, tmp_path):
    """P changed discount_rate under a root that names the user's sentence; a
    meeting passage hangs on that sentence. P0 left a per-node reason on export
    from the old close."""
    graph = tmp_path / "proj-state-graph.db"
    c = ps.build(graph)
    ps.add_run(c, 1, plan_id="P0")
    for i, (k, qn) in enumerate(NODES.items(), 1):
        ps.add_snapshot(c, 1, k, qn)
        ps.add_event(c, 1, i, "node_added", k)
    ps.add_run(c, 2, plan_id="P", step_id="P-REVIEW.1")
    ps.add_snapshot(c, 2, "nk_a", NODES["nk_a"], struct_sig="s2")
    ps.add_event(c, 2, 1, "node_changed", "nk_a", '{"changed": ["struct_sig"]}')
    c.commit()
    c.close()
    graph = str(graph)
    for pid in ("P0", "P"):
        db.insert_plan(conn, pid, f"goal of {pid}", project="proj", project_source="declared")
    said = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P", text=SAID,
                               occurred_at="2026-05-20 11:20:00", origin="hook")
    roots.record(conn, plan_id="P", root={"kind": "new", "utterance_id": said})
    (task,) = reasons.fill_tasks(conn, project="proj", plan_id="P", run_id=2, psg_db_path=graph,
                                 tasks=[{"interpretation": TASK}])["reason_ids"]
    ref = pv.insert_reference(conn, project="proj", kind="meeting", label="data platform sync, 2026-05-19",
                              occurred_at="2026-05-19 15:00", uri="notes/2026-05-19-data-platform-sync.md",
                              said_by="the data platform lead", excerpt=EXCERPT)
    pv.link_reference(conn, None, ref, utterance_id=said)
    old = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_d", kind="technical",
                           interpretation=OLD, recorded_by="agent")
    return {"graph": graph, "task": task, "ref": ref, "old": old}


def _facts_text(conn, world, qn):
    ft = facts.facts(conn, world["graph"], [qn], project="proj")
    return ft, facts.render(ft)


# ── the source reached through the root, end to end ─────────────────────────

def test_the_fact_table_shows_the_roots_source_with_who_said_it_and_the_passage(conn, world):
    _, text = _facts_text(conn, world, "pkg.m.discount_rate")
    assert TASK in text and f"[#r{world['ref']}]" in text
    assert "the data platform lead" in text and EXCERPT in text
    assert "the root's words" in text, "say what the source backs: the root, not this node"


def test_the_receipts_timeline_carries_the_source_and_its_passage(conn, world):
    ft, _ = _facts_text(conn, world, "pkg.m.discount_rate")
    lines = [e["text"] for e in receipts.timeline(conn, ft)]
    assert any("the data platform lead" in t and EXCERPT[:40] in t for t in lines), lines


def test_a_source_without_a_passage_still_says_the_body_is_not_kept(conn, world):
    ref = pv.insert_reference(conn, project="proj", kind="email", label="Re: feed v2", occurred_at="2026-05-20",
                              uri="mailto:platform@example.invalid")
    pv.link_reference(conn, world["task"], ref)
    _, text = _facts_text(conn, world, "pkg.m.discount_rate")
    line = next(l for l in text.splitlines() if f"[#r{ref}]" in l)
    assert "the body is not in the ledger" in line


# ── old per-node records say what they are ──────────────────────────────────

def test_why_marks_an_old_per_node_close_record(conn, world):
    text = why.why(conn, project="proj", target="pkg.m.export", psg_db_path=world["graph"], record=False)["text"]
    line = next(l for l in text.splitlines() if f"#{world['old']} " in l)
    assert MARK in line and OLD in text


def test_the_fact_table_and_the_receipts_mark_it_too(conn, world):
    ft, text = _facts_text(conn, world, "pkg.m.export")
    head = next(l for l in text.splitlines() if f"[#{world['old']}]" in l)
    assert MARK in head
    lines = [e["text"] for e in receipts.timeline(conn, ft)]
    assert any(OLD in t and MARK in t for t in lines), lines


@pytest.mark.parametrize("who", ["a person's note", "the external trigger"])
def test_a_note_and_the_external_triggers_row_are_not_marked(conn, world, who):
    if who == "a person's note":
        u = pv.insert_utterance(conn, session_id="note", project="proj", plan_id=None, text="export stays daily",
                                occurred_at="2026-05-01 09:00:00")
        rid = pv.insert_reason(conn, project="proj", plan_id="note", node_key="nk_d", kind="technical",
                               verbatim=(u, 0, 18), recorded_by="human")
    else:
        u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0", text="the uplift is 0.12",
                                occurred_at="2026-05-01 09:00:00")
        rid = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_d", kind="organizational",
                               verbatim=(u, 0, 18), rule_id=external_trigger.RULE_ID, recorded_by="system")
    text = why.why(conn, project="proj", target="pkg.m.export", psg_db_path=world["graph"], record=False)["text"]
    line = next(l for l in text.splitlines() if l.lstrip().startswith(f"#{rid} "))
    assert MARK not in line


def test_the_predicate_is_one_function():
    row = {"role": "reason", "node_key": "nk_a", "recorded_by": "system", "rule_id": "R4"}
    assert reasons.per_node_close_record(row) is True
    assert reasons.per_node_close_record({**row, "node_key": None}) is False
    assert reasons.per_node_close_record({**row, "role": "constraint"}) is False
    assert reasons.per_node_close_record({**row, "recorded_by": "human"}) is False
    assert reasons.per_node_close_record({**row, "rule_id": external_trigger.RULE_ID}) is False
