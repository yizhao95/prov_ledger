"""orchestrator.provenance — the store layer: tier derived from what a reason
points at, stated only through an utterance span, one hash chain per table."""
import sqlite3

import pytest

from orchestrator import provenance as pv

AT = "2026-09-15 10:00:00"


def _utt(conn, text="please keep fiscal weeks, not calendar weeks"):
    return pv.insert_utterance(conn, session_id="s", project="p", plan_id="P1", text=text, occurred_at=AT)


def _reason(conn, **kw):
    base = dict(project="p", plan_id="P1", node_key="nk_a", kind="technical")
    base.update(kw)
    return pv.insert_reason(conn, **base)


def test_tier_is_derived_from_the_inputs_four_ways(conn):
    u = _utt(conn)
    s = _reason(conn, verbatim=(u, 0, 24))
    a = _reason(conn, interpretation="weekly grain because finance reconciles weekly")
    d = _reason(conn, rule_id="R4", interpretation="identity kept, struct_sig unchanged", recorded_by="system")
    n = _reason(conn, recorded_by="system")
    tiers = {i: pv.get_reason(conn, i)["tier"] for i in (s, a, d, n)}
    assert tiers == {s: "stated", a: "asserted", d: "derived", n: "unstated"}
    assert pv.get_reason(conn, s)["verbatim_end"] == 24 and pv.get_reason(conn, s)["evidence_level"] == "verbal"
    assert pv.get_reason(conn, a)["evidence_level"] == "task_context" and pv.get_reason(conn, n)["evidence_level"] == "unstated"


def test_tier_cannot_be_passed(conn):
    with pytest.raises(TypeError):
        _reason(conn, tier="stated", interpretation="x")
    with pytest.raises(TypeError):
        _reason(conn, tier="derived")


@pytest.mark.veto
def test_a2_a4_bare_text_can_never_become_stated(conn):
    """Every path that accepts free text tops out at asserted: only a span of a
    recorded utterance is stated, and it is checked against the utterance."""
    a = _reason(conn, interpretation="the user said fiscal weeks", recorded_by="human")
    b = _reason(conn, statement="use fiscal weeks", rationale="finance", recorded_by="human", role="constraint", kind="organizational")
    assert {pv.get_reason(conn, a)["tier"], pv.get_reason(conn, b)["tier"]} == {"asserted"}
    assert conn.execute("SELECT COUNT(*) FROM change_reason WHERE tier='stated'").fetchone()[0] == 0
    u = _utt(conn)
    with pytest.raises(ValueError, match="outside"):
        _reason(conn, verbatim=(u, 0, 999))
    with pytest.raises(ValueError, match="outside"):
        _reason(conn, verbatim=(u, 5, 5))
    with pytest.raises(ValueError, match="does not exist"):
        _reason(conn, verbatim=(u + 100, 0, 3))
    assert conn.execute("SELECT COUNT(*) FROM change_reason WHERE tier='stated'").fetchone()[0] == 0


def test_recorded_at_is_not_accepted_and_is_the_db_clock(conn):
    with pytest.raises(TypeError):
        _reason(conn, interpretation="x", recorded_at="2020-01-01 00:00:00")
    with pytest.raises(TypeError):
        pv.insert_utterance(conn, session_id="s", project="p", plan_id=None, text="t", occurred_at=AT, recorded_at="2020-01-01")
    r = _reason(conn, interpretation="x", occurred_at="2020-01-01 00:00:00")
    row = pv.get_reason(conn, r)
    assert row["occurred_at"] == "2020-01-01 00:00:00" and row["recorded_at"] != row["occurred_at"]
    assert len(row["recorded_at"]) == 19


def test_reference_verifiability_and_links(conn):
    linked = pv.insert_reference(conn, project="p", kind="email", label="re: weeks", occurred_at=AT, uri="mail:1")
    unreach = pv.insert_reference(conn, project="p", kind="email", label="re: weeks (lost)", occurred_at=AT)
    verbal = pv.insert_reference(conn, project="p", kind="verbal", label="hallway, Tue", occurred_at=AT)
    vers = dict(conn.execute("SELECT id, verifiability FROM reference").fetchall())
    assert vers == {linked: "linked", unreach: "unreachable", verbal: "verbal"}
    with pytest.raises(ValueError, match="verbal"):
        pv.insert_reference(conn, project="p", kind="verbal", label="x", occurred_at=AT, uri="http://x")
    with pytest.raises(ValueError, match="512"):
        pv.insert_reference(conn, project="p", kind="doc", label="x" * 513, occurred_at=AT)
    a = _reason(conn, interpretation="finance asked", refs=[linked])
    b = _reason(conn, interpretation="finance asked", refs=[verbal])
    c = _reason(conn, interpretation="finance asked", refs=[unreach])
    lv = {i: pv.get_reason(conn, i)["evidence_level"] for i in (a, b, c)}
    assert lv == {a: "linked", b: "verbal", c: "task_context"}


def test_hash_chain_verifies_and_names_the_tampered_row(conn):
    ids = [_utt(conn, f"utterance {i}") for i in range(3)]
    assert pv.verify_chain(conn, "utterance") == {"ok": True, "rows": 3, "first_bad_id": None}
    rows = conn.execute("SELECT id, prev_hash, hash FROM utterance ORDER BY id").fetchall()
    assert rows[0][1] is None and rows[1][1] == rows[0][2] and rows[2][1] == rows[1][2]
    for i in range(3):
        _reason(conn, interpretation=f"r{i}")
    assert pv.verify_chain(conn, "change_reason")["ok"] is True
    conn.execute("DROP TRIGGER trg_utterance_no_update")           # simulate someone editing the file directly
    conn.execute("UPDATE utterance SET text='rewritten' WHERE id=?", (ids[1],))
    conn.commit()
    assert pv.verify_chain(conn, "utterance") == {"ok": False, "rows": 2, "first_bad_id": ids[1]}
    with pytest.raises(ValueError):
        pv.verify_chain(conn, "reference_link")


def test_supersede_is_the_only_update_and_keeps_the_old_row(conn):
    old = _reason(conn, interpretation="v1")
    new = _reason(conn, interpretation="v2")
    pv.supersede(conn, old, new)
    o = pv.get_reason(conn, old)
    assert o["superseded_by"] == new and o["state"] == "active" and o["interpretation"] == "v1"
    assert pv.verify_chain(conn, "change_reason")["ok"] is True       # superseded_by is outside the chain on purpose
    with pytest.raises(ValueError):
        pv.supersede(conn, old, old)
    with pytest.raises(ValueError):
        pv.supersede(conn, old, 9999)


def test_reason_validation(conn):
    with pytest.raises(ValueError, match="node_key"):
        _reason(conn, node_key=None, interpretation="x")
    r = _reason(conn, node_key=None, role="rejected_path", interpretation="tried X, failed")
    assert pv.get_reason(conn, r)["role"] == "rejected_path"
    with pytest.raises(ValueError, match="rationale"):
        _reason(conn, rationale="a why without a what")
    with pytest.raises(ValueError):
        _reason(conn, kind="emotional", interpretation="x")
    assert [x["id"] for x in pv.reasons_for_plan(conn, "P1", role="rejected_path")] == [r]
    assert pv.reasons_for_node(conn, "nk_zzz") == []
