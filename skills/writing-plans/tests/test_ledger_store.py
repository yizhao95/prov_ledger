"""Tests for ledger_store — provLedger Phase E manual decision-memory store.

ledger_store is stdlib-only (sqlite3+json) and imports nothing from the
orchestrator package. Tests build a migrated DB via the orchestrator's
run_migrations (so the real LedgerEntries schema is exercised).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
ORCH_ROOT = Path.home() / "skill-workspace" / "orchestrator"
sys.path.insert(0, str(ORCH_ROOT))

import ledger_store  # noqa: E402
from orchestrator import db as orch_db  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "led.db"))
    c.row_factory = sqlite3.Row
    orch_db.run_migrations(c)
    return c


def test_add_entry_round_trip(conn):
    eid = ledger_store.add_entry(
        conn, project="proj", kind="decision",
        statement="rolling-window split, not random",
        rationale="random split leaks temporal info",
        subjects=["train_test_split", "split"],
        keywords=["split", "rolling", "temporal"],
        source="manual")
    assert isinstance(eid, int) and eid > 0
    rows = ledger_store.get_entries(conn, "proj")
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "decision"
    assert r["statement"].startswith("rolling-window")
    assert "split" in r["subjects"]
    assert "temporal" in r["keywords"]


def test_invalid_kind_rejected(conn):
    with pytest.raises(ValueError):
        ledger_store.add_entry(conn, project="p", kind="bogus",
                               statement="s", rationale="r")


def test_active_only_by_default(conn):
    a = ledger_store.add_entry(conn, project="p", kind="decision",
                               statement="keep", rationale="r")
    b = ledger_store.add_entry(conn, project="p", kind="anti_pattern",
                               statement="drop", rationale="r")
    ledger_store.supersede_entry(conn, b)
    active = ledger_store.get_entries(conn, "p")
    assert {e["id"] for e in active} == {a}
    allrows = ledger_store.get_entries(conn, "p", include_superseded=True)
    assert {e["id"] for e in allrows} == {a, b}


def test_query_entries_deterministic_order(conn):
    ids = [ledger_store.add_entry(conn, project="p", kind="decision",
                                  statement=f"s{i}", rationale="r") for i in range(3)]
    rows = ledger_store.query_entries(conn, "p")
    # deterministic (by created_at then id); all present
    assert {r["id"] for r in rows} == set(ids)
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)


def test_other_project_isolated(conn):
    ledger_store.add_entry(conn, project="p1", kind="decision",
                           statement="s", rationale="r")
    assert ledger_store.get_entries(conn, "p2") == []
