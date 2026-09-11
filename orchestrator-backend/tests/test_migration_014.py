"""Migration 014 — node_reason / expectations / outcomes (append-only) + Plans.review_skip_reason."""
import sqlite3

import pytest

from orchestrator import api, db


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _reason(**kw):
    base = dict(node_key="nk_a", project="p", run_id=1, plan_id="P1", kind="reason", text="because",
                source="agent", tier="stated")
    base.update(kw)
    return base


def test_014_tables_and_columns(conn):
    assert {"node_key", "project", "run_id", "plan_id", "step_id", "kind", "text", "source", "tier"} <= _cols(conn, "node_reason")
    assert {"target", "target_kind", "claim", "channel"} <= _cols(conn, "expectations")
    assert {"expectation_id", "kind", "value_json", "tier", "reason", "backfilled_by_plan"} <= _cols(conn, "outcomes")
    assert "review_skip_reason" in _cols(conn, "Plans")
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for t in ("node_reason", "expectations", "outcomes"):
        assert {f"trg_{t}_no_update", f"trg_{t}_no_delete"} <= triggers


def test_014_node_reason_append_only(conn):
    rid = db.insert_node_reason(conn, **_reason())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE node_reason SET text='x' WHERE id=?", (rid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM node_reason WHERE id=?", (rid,))


def test_014_unstated_is_null_text_not_a_string(conn):
    db.insert_node_reason(conn, **_reason(text=None))
    rows = db.get_node_reasons(conn, node_key="nk_a")
    assert len(rows) == 1 and rows[0]["text"] is None and rows[0]["kind"] == "reason"


def test_014_checks(conn):
    for bad in (dict(kind="guess"), dict(source="llm"), dict(tier="observed")):
        with pytest.raises((sqlite3.IntegrityError, ValueError)):
            db.insert_node_reason(conn, **_reason(**bad))
    # rejected_path may be unanchored; other kinds may not
    db.insert_node_reason(conn, **_reason(node_key=None, kind="rejected_path", text="tried X", tier="asserted"))
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_node_reason(conn, **_reason(node_key=None))
    assert [r["kind"] for r in db.get_node_reasons(conn, plan_id="P1")] == ["rejected_path"]


def test_014_get_node_reasons_filters(conn):
    db.insert_node_reason(conn, **_reason(node_key="nk_a", plan_id="P1"))
    db.insert_node_reason(conn, **_reason(node_key="nk_b", plan_id="P1"))
    db.insert_node_reason(conn, **_reason(node_key="nk_a", plan_id="P2", step_id="P2-A"))
    assert [r["plan_id"] for r in db.get_node_reasons(conn, node_key="nk_a")] == ["P1", "P2"]
    assert [r["node_key"] for r in db.get_node_reasons(conn, plan_id="P1")] == ["nk_a", "nk_b"]
    assert db.get_node_reasons(conn, node_key="nk_a", plan_id="P2")[0]["step_id"] == "P2-A"
    assert len(db.get_node_reasons(conn)) == 3


def test_014_expectation_outcome_roundtrip(conn):
    eid = db.insert_expectation(conn, plan_id="P1", step_id=None, project="p", target="orders.amount",
                                target_kind="column", claim="amount no longer null", channel="profile_drift")
    pending = db.get_pending_expectations(conn, "p", exclude_plan_id="P2", kind="observed")
    assert [e["id"] for e in pending] == [eid] and pending[0]["target_kind"] == "column"
    assert db.get_pending_expectations(conn, "p", exclude_plan_id="P1", kind="observed") == []   # own plan excluded
    oid = db.insert_outcome(conn, expectation_id=eid, kind="observed", value={"drifts": []},
                            source="data_profile", tier="observed", backfilled_by_plan="P2")
    assert db.get_pending_expectations(conn, "p", exclude_plan_id="P2", kind="observed") == []
    assert db.get_pending_expectations(conn, "p", exclude_plan_id="P2", kind="survival")[0]["id"] == eid   # other kind still pending
    out = db.get_outcomes(conn, eid)
    assert out[0]["id"] == oid and out[0]["value_json"] == '{"drifts": []}' and out[0]["backfilled_by_plan"] == "P2"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM outcomes")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE expectations SET claim='x'")
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_expectation(conn, plan_id="P1", step_id=None, project="p", target="t", target_kind="table",
                              claim="c", channel="none")
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_outcome(conn, expectation_id=eid, kind="guess", value={}, source="s", tier="observed")


def test_014_review_skip_reason_roundtrip(conn):
    r = api.initialize_plan(conn, "g", ["a"])
    assert db.get_plan(conn, r["plan_id"])["review_skip_reason"] is None
    db.set_review_skip_reason(conn, r["plan_id"], "no registered project mentioned")
    assert db.get_plan(conn, r["plan_id"])["review_skip_reason"].startswith("no registered")


def test_014_migration_is_idempotent(tmp_path):
    c = db.open_db(tmp_path / "x.db")
    n1 = db.run_migrations(c)
    n2 = db.run_migrations(c)
    assert n1 >= 14 and n2 == 0
    c.close()
