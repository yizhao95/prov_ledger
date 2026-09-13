"""Migration 016 — Plans.project / project_source (FL-014): explicit attribution, assigned once."""
import sqlite3

import pytest

from orchestrator import db


def test_016_plans_project_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(Plans)")}
    assert {"project", "project_source"} <= cols
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='Plans'")}
    assert "idx_plans_project" in names


def test_insert_plan_with_project(conn):
    db.insert_plan(conn, "P1", "g", project="prov_ledger", project_source="cwd")
    p = db.get_plan(conn, "P1")
    assert (p["project"], p["project_source"]) == ("prov_ledger", "cwd")
    db.insert_plan(conn, "P2", "g")
    assert (db.get_plan(conn, "P2")["project"], db.get_plan(conn, "P2")["project_source"]) == (None, None)


def test_project_assigned_once(conn):
    db.insert_plan(conn, "P1", "g")
    db.set_plan_project(conn, "P1", "prov_ledger", "declared")
    assert db.get_plan(conn, "P1")["project_source"] == "declared"
    with pytest.raises(ValueError):
        db.set_plan_project(conn, "P1", "other", "declared")
    assert db.get_plan(conn, "P1")["project"] == "prov_ledger"


def test_project_none_is_a_legal_value(conn):
    db.insert_plan(conn, "P1", "g", project="none", project_source="declared")
    assert db.get_plan(conn, "P1")["project"] == "none"


def test_bad_project_source_rejected(conn):
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        db.insert_plan(conn, "P1", "g", project="x", project_source="guess")
    db.insert_plan(conn, "P2", "g")
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        db.set_plan_project(conn, "P2", "x", "guess")
    assert db.PROJECT_SOURCES == ("declared", "cwd", "legacy")
