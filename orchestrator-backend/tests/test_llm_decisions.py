"""Phase 1.3 — llm_decisions table + auto-sync to the ledger."""
from __future__ import annotations

import json
import sqlite3

from orchestrator import db


def test_013_schema(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_decisions)").fetchall()}
    assert cols >= {"project", "plan_id", "step_id", "dataset", "column_name",
                    "observed_before", "observed_after", "drift_kind", "decision",
                    "rationale", "action", "outcome", "human_feedback", "created_at"}


def test_failure_decision_records_row_and_ledger_antipattern(conn):
    did = db.insert_llm_decision(
        conn, project="proj", plan_id="p1", step_id="s1",
        dataset="train", column="label", drift_kind="dtype_changed",
        observed_before="int64", observed_after="object",
        decision="coerce label back to int upstream",
        rationale="model.predict needs numeric labels; the string cast was a bug",
        action="coerce_upstream", outcome="resolved", failure=True)
    assert isinstance(did, int) and did > 0

    rows = db.get_llm_decisions(conn, dataset="train", project="proj")
    assert len(rows) == 1 and rows[0]["action"] == "coerce_upstream"

    # auto-synced ledger entry (anti-pattern, since failure=True)
    led = conn.execute(
        "SELECT kind, statement, subjects, keywords, source, plan_id "
        "FROM LedgerEntries WHERE source='llm_decision'").fetchall()
    assert len(led) == 1
    assert led[0]["kind"] == "anti_pattern"
    assert led[0]["statement"].startswith("coerce label")
    assert "label" in json.loads(led[0]["subjects"])
    assert "dtype_changed" in json.loads(led[0]["keywords"])
    assert led[0]["plan_id"] == "p1"


def test_non_failure_decision_is_ledger_decision_kind(conn):
    db.insert_llm_decision(
        conn, dataset="d", column="c", decision="adapt downstream encoder",
        rationale="c is now categorical by design", action="adapt_downstream",
        failure=False)
    kind = conn.execute(
        "SELECT kind FROM LedgerEntries WHERE source='llm_decision'").fetchone()[0]
    assert kind == "decision"


def test_human_feedback_persisted(conn):
    db.insert_llm_decision(
        conn, dataset="d", column="c", decision="adapt downstream",
        action="adapt_downstream", human_feedback="user said: x is categorical now")
    row = db.get_llm_decisions(conn, dataset="d")[0]
    assert "categorical" in row["human_feedback"]
