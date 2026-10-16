"""`provledger record` prints what a source said, not only where it is.

Since migration 037 a reference keeps who said it (`said_by`) and the passage
(`excerpt`): the words a reason rests on once the link has rotted. The record
read is the whole, uncut one, so a passage the ledger holds must reach it — on
the source itself and in the source list of every record it backs. A passage on
a `personal` source stays in the ledger and is not shown by any read; the read
says it was withheld instead of leaving a blank. A task reason hangs on the
plan, not on a node, and is called that."""
from orchestrator import db, provenance as pv, record_read

AT = "2026-09-14 16:00:00"
SAID = "leave the region out of the Q3 rollup until the audit closes"


def _seed(conn):
    db.insert_plan(conn, "P1", "drop the region", project="proj", project_source="declared")
    rid = pv.insert_reason(conn, project="proj", plan_id="P1", node_key=None, kind="organizational",
                           interpretation="the finance lead asked to leave the region out until the audit closes")
    shared = pv.insert_reference(conn, project="proj", kind="email", label="Q3 scope · the finance lead",
                                 occurred_at=AT, uri="https://mail.example.com/m/1", said_by="the finance lead",
                                 excerpt=SAID)
    private = pv.insert_reference(conn, project="proj", kind="chat", label="side thread · a colleague",
                                  occurred_at=AT, uri="https://chat.example.com/t/2", said_by="a colleague",
                                  excerpt="between us: the audit is late", visibility="personal")
    pv.link_reference(conn, rid, shared)
    pv.link_reference(conn, rid, private)
    return rid, shared, private


def test_a_record_lists_who_said_each_source_and_the_passage(conn):
    rid, shared, _ = _seed(conn)
    text = record_read.render(record_read.record(conn, f"#{rid}"))
    assert f"#r{shared}" in text and "said by the finance lead" in text and SAID in text


def test_a_source_read_prints_who_said_it_and_the_passage(conn):
    _, shared, _ = _seed(conn)
    doc = record_read.record(conn, f"#r{shared}")
    assert (doc["said_by"], doc["excerpt"]) == ("the finance lead", SAID)
    text = record_read.render(doc)
    assert "said by the finance lead" in text and SAID in text


def test_a_personal_passage_is_withheld_and_the_read_says_so(conn):
    rid, _, private = _seed(conn)
    for cite in (f"#{rid}", f"#r{private}"):
        text = record_read.render(record_read.record(conn, cite))
        assert "between us" not in text
        assert "passage withheld" in text


def test_a_task_reason_is_not_called_a_floating_rejected_path(conn):
    rid, _, _ = _seed(conn)
    text = record_read.render(record_read.record(conn, f"#{rid}"))
    assert "floating rejected path" not in text
    assert "task reason of plan P1" in text
