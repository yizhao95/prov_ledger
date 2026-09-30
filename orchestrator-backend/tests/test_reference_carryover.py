"""A3, the other half: a pointer pinned to the words follows them to the reason.

The UserPromptSubmit hint hangs a pointer on an `utterance`, because at the moment
the sentence is typed there is no plan and therefore no reason for it to hang on.
`change_reason_v.evidence_level` only counts pointers anchored to `reference_link.
reason_id`, so until the reason quoting those words carries them too, the cheap
path raises nothing at all and the source level stays `verbal`.

Carrying is a copy, never a move: `reference_link` is append-only, and "this
pointer was captured at the time these words were said" is a fact in its own
right. The stance comes with it — a pointer that contradicted the source it was
found for still contradicts it after the reason is written.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from orchestrator import provenance as pv, reasons
from orchestrator.ask import locate

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

AT = "2026-09-26 09:00:00"
SAID = "drop EMEA from the Q3 rollup, Sarah emailed that yesterday"
KEYS = (("nk_a", "pkg.rollup.weekly_report"), ("nk_b", "pkg.rollup.clean_regions"))


@pytest.fixture
def psg(tmp_path):
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    for i, (k, qn) in enumerate(KEYS, 1):
        ps.add_snapshot(c, 1, k, qn)
        ps.add_event(c, 1, i, "node_added", k)
    ps.add_run(c, 2, plan_id="P1", step_id="P1-REVIEW.1")
    for i, (k, qn) in enumerate(KEYS, 1):
        ps.add_snapshot(c, 2, k, qn)
        ps.add_event(c, 2, i, "node_changed", k, '{"changed": ["struct_sig"]}')
    c.commit()
    c.close()
    return str(path)


@pytest.fixture
def spoken(conn):
    """The cheap path as it really happens: the words come in through the hook and
    the host agent hangs the pointer it found on the utterance, before any plan."""
    uid = pv.insert_utterance(conn, session_id="s1", project="proj", plan_id=None, text=SAID,
                              occurred_at=AT, origin="hook")
    ref = pv.insert_reference(conn, project="proj", kind="email", label="re: Q3 scope · sarah@example.com",
                              occurred_at=AT, uri="https://outlook/items/AAQk")
    pv.link_reference(conn, None, ref, utterance_id=uid)
    return {"utterance": uid, "reference": ref}


def _fill(conn, psg, uid, span=(0, 26), node_key="nk_a"):
    return reasons.fill(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg,
                        reasons=[{"node_key": node_key, "utterance_id": uid, "span": list(span)}])


def _links(conn, reference_id):
    return [dict(r) for r in conn.execute(
        "SELECT reason_id, utterance_id, stance FROM reference_link WHERE reference_id = ? "
        "ORDER BY COALESCE(reason_id, 0), COALESCE(utterance_id, 0)", (reference_id,))]


def _reason_of(conn, node_key="nk_a"):
    return conn.execute("SELECT id FROM change_reason WHERE plan_id = 'P1' AND node_key = ? ORDER BY id DESC LIMIT 1",
                        (node_key,)).fetchone()[0]


# ── (a) the pointer follows the words ───────────────────────────────────────

def test_a_stated_reason_gains_a_link_to_every_pointer_its_words_carry(conn, psg, spoken):
    out = _fill(conn, psg, spoken["utterance"])
    assert out["stated"] == 1, out
    rid = _reason_of(conn)
    got = _links(conn, spoken["reference"])
    assert {"reason_id": rid, "utterance_id": None, "stance": "supports"} in got


def test_the_utterance_anchored_row_is_not_deleted(conn, psg, spoken):
    """Append-only, and the fact is worth keeping on its own: someone produced this
    pointer at the moment the sentence was said, not months later at review."""
    _fill(conn, psg, spoken["utterance"])
    got = _links(conn, spoken["reference"])
    assert {"reason_id": None, "utterance_id": spoken["utterance"], "stance": "supports"} in got
    assert len(got) == 2, got


def test_the_reason_becomes_linked(conn, psg, spoken):
    rid_before = None
    _fill(conn, psg, spoken["utterance"])
    rid = _reason_of(conn)
    row = pv.get_reason(conn, rid)
    assert row["tier"] == "stated"
    assert row["evidence_level"] == "linked", "the pointer the words carried must raise the source level"
    assert rid_before is None


def test_an_unreachable_pointer_carries_across_without_claiming_to_be_linked(conn, psg):
    """No uri yet. The pointer still belongs on the reason; it just does not
    pretend the source can be opened."""
    uid = pv.insert_utterance(conn, session_id="s1", project="proj", plan_id=None, text=SAID,
                              occurred_at=AT, origin="hook")
    ref = pv.insert_reference(conn, project="proj", kind="email", label="re: Q3 scope · sarah", occurred_at=AT)
    pv.link_reference(conn, None, ref, utterance_id=uid)
    _fill(conn, psg, uid)
    rid = _reason_of(conn)
    assert [l["reason_id"] for l in _links(conn, ref) if l["reason_id"]] == [rid]
    assert pv.get_reason(conn, rid)["evidence_level"] == "verbal"


def test_every_pointer_the_words_carry_comes_across_not_just_the_first(conn, psg, spoken):
    second = pv.insert_reference(conn, project="proj", kind="meeting", label="Q3 planning · 2026-09-24",
                                 occurred_at=AT, uri="https://teams/meet/1")
    third = pv.insert_reference(conn, project="proj", kind="ticket", label="DATA-42", occurred_at=AT,
                                uri="https://tracker/DATA-42")
    pv.link_reference(conn, None, second, utterance_id=spoken["utterance"])
    pv.link_reference(conn, None, third, utterance_id=spoken["utterance"], stance="context")
    _fill(conn, psg, spoken["utterance"])
    rid = _reason_of(conn)
    carried = {r[0]: r[1] for r in conn.execute(
        "SELECT reference_id, stance FROM reference_link WHERE reason_id = ?", (rid,))}
    assert carried == {spoken["reference"]: "supports", second: "supports", third: "context"}


# ── (b) a contradicting pointer keeps contradicting ─────────────────────────

def test_a_contradicting_pointer_carries_across_with_its_stance(conn, psg):
    uid = pv.insert_utterance(conn, session_id="s1", project="proj", plan_id=None, text=SAID,
                              occurred_at=AT, origin="hook")
    ref = pv.insert_reference(conn, project="proj", kind="email", label="re: EMEA is in Q3 after all · sarah",
                              occurred_at=AT, uri="https://outlook/items/BBQk")
    pv.link_reference(conn, None, ref, utterance_id=uid, stance="contradicts")
    _fill(conn, psg, uid)
    rid = _reason_of(conn)
    assert conn.execute("SELECT stance FROM reference_link WHERE reason_id = ? AND reference_id = ?",
                        (rid, ref)).fetchone()[0] == "contradicts"
    # the tier is what the user said; a contradicting source does not demote it
    assert pv.get_reason(conn, rid)["tier"] == "stated"


# ── (c) idempotent, and silent when there is nothing to carry ───────────────

def test_carrying_twice_does_not_duplicate(conn, psg, spoken):
    _fill(conn, psg, spoken["utterance"])
    rid = _reason_of(conn)
    n = pv.carry_utterance_references(conn, reason_id=rid, utterance_id=spoken["utterance"])
    assert n == 0, "the pointer is already on this reason"
    assert conn.execute("SELECT COUNT(*) FROM reference_link WHERE reason_id = ? AND reference_id = ?",
                        (rid, spoken["reference"])).fetchone()[0] == 1
    assert len(_links(conn, spoken["reference"])) == 2


def test_the_first_carry_reports_how_many_it_moved_across(conn, psg, spoken):
    rid = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_b", kind="technical",
                           interpretation="a reading, with no words behind it")
    assert pv.carry_utterance_references(conn, reason_id=rid, utterance_id=spoken["utterance"]) == 1
    assert pv.carry_utterance_references(conn, reason_id=rid, utterance_id=spoken["utterance"]) == 0


def test_words_carrying_no_pointer_carry_nothing_and_raise_nothing(conn, psg):
    uid = pv.insert_utterance(conn, session_id="s1", project="proj", plan_id=None, text=SAID,
                              occurred_at=AT, origin="hook")
    out = _fill(conn, psg, uid)
    assert out["stated"] == 1
    rid = _reason_of(conn)
    assert conn.execute("SELECT COUNT(*) FROM reference_link").fetchone()[0] == 0
    assert pv.get_reason(conn, rid)["evidence_level"] == "verbal"


def test_an_asserted_answer_carries_nothing_because_it_quotes_nobody(conn, psg, spoken):
    reasons.fill(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg,
                 reasons=[{"node_key": "nk_a", "interpretation": "EMEA excluded from the Q3 rollup"}])
    rid = _reason_of(conn)
    assert conn.execute("SELECT COUNT(*) FROM reference_link WHERE reason_id = ?", (rid,)).fetchone()[0] == 0
    assert pv.get_reason(conn, rid)["evidence_level"] == "task_context"


def test_an_unstated_answer_carries_nothing(conn, psg, spoken):
    reasons.fill(conn, project="proj", plan_id="P1", run_id=2, psg_db_path=psg,
                 reasons=[{"node_key": "nk_a", "unstated": True}])
    rid = _reason_of(conn)
    assert conn.execute("SELECT COUNT(*) FROM reference_link WHERE reason_id = ?", (rid,)).fetchone()[0] == 0


def test_two_reasons_quoting_the_same_words_each_get_their_own_link(conn, psg, spoken):
    _fill(conn, psg, spoken["utterance"], node_key="nk_a")
    _fill(conn, psg, spoken["utterance"], node_key="nk_b")
    rids = {_reason_of(conn, "nk_a"), _reason_of(conn, "nk_b")}
    got = {l["reason_id"] for l in _links(conn, spoken["reference"]) if l["reason_id"]}
    assert got == rids
    for rid in rids:
        assert pv.get_reason(conn, rid)["evidence_level"] == "linked"


def test_the_carry_refuses_a_reason_that_does_not_exist(conn, spoken):
    with pytest.raises(ValueError, match="9999"):
        pv.carry_utterance_references(conn, reason_id=9999, utterance_id=spoken["utterance"])


# ── (d) /ledger can see a pointer that never left the utterance ─────────────

@pytest.fixture
def pinned_after(conn):
    """The reason is already recorded, and only THEN does someone pin the source to
    the words it quotes — `reference add --utterance` after the fact. Nothing
    carried it across, so the pointer sits on the utterance alone: exactly the row
    that used to be invisible to /ledger."""
    uid = pv.insert_utterance(conn, session_id="s1", project="proj", plan_id="P1", text=SAID,
                              occurred_at=AT, origin="hook")
    rid = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical",
                           verbatim=(uid, 0, 26))
    ref = pv.insert_reference(conn, project="proj", kind="email", label="re: Q3 scope · sarah@example.com",
                              occurred_at=AT, uri="https://outlook/items/AAQk")
    pv.link_reference(conn, None, ref, utterance_id=uid)
    assert conn.execute("SELECT COUNT(*) FROM reference_link WHERE reason_id IS NOT NULL").fetchone()[0] == 0
    return {"utterance": uid, "reason": rid, "reference": ref}


def test_reference_hits_find_a_pointer_still_anchored_to_the_words(conn, pinned_after):
    """Without this, a source captured on the words is invisible to every question
    asked later — the opposite of why it was captured early."""
    hits = locate._reference_hits(conn, "proj", ["q3"], 20)
    assert (pinned_after["reason"], "nk_a", pinned_after["reference"]) in [(h[0], h[1], h[2]) for h in hits], hits


def test_reference_hits_still_find_a_pointer_anchored_to_the_reason(conn, spoken):
    rid = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_b", kind="technical",
                           interpretation="a reading")
    pv.link_reference(conn, rid, spoken["reference"])
    hits = locate._reference_hits(conn, "proj", ["q3"], 20)
    assert (rid, "nk_b", spoken["reference"]) in [(h[0], h[1], h[2]) for h in hits]


def test_a_pointer_carried_across_is_not_counted_twice(conn, psg, spoken):
    _fill(conn, psg, spoken["utterance"])
    rid = _reason_of(conn)
    hits = [h for h in locate._reference_hits(conn, "proj", ["q3"], 20) if h[0] == rid]
    assert len(hits) == 1, hits


def test_the_candidate_list_says_where_the_source_came_from(conn, tmp_path, pinned_after):
    graph = tmp_path / "g.db"
    c = ps.build(graph)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_snapshot(c, 1, "nk_a", "pkg.rollup.weekly_report")
    c.commit(); c.close()
    cands = locate.candidates(conn, str(graph), "why was the Q3 scope chosen?", project="proj")
    why = " ".join(c["why"] for c in cands)
    assert "source #" in why, why
    assert f"source #{pinned_after['reference']}" in why, why
