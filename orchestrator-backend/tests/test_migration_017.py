"""Migration 017 — metrics: append-only numeric observations a metric channel judges expectations against."""
import sqlite3

import pytest

from orchestrator import db


def test_017_metrics_table_and_index(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(metrics)")}
    assert {"id", "project", "plan_id", "step_id", "name", "value", "unit", "source", "observed_at"} <= cols
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='metrics'")}
    assert "idx_metrics_project_name" in names


def test_insert_and_get_metrics_windowed(conn):
    a = db.insert_metric(conn, project="p", name="ctr", value=1.5, unit="pct", plan_id="P1", step_id="P1-A", source="demo",
                         observed_at="2026-09-11 00:30:00")
    b = db.insert_metric(conn, project="p", name="ctr", value=2.0, observed_at="2026-09-11 02:00:00")
    db.insert_metric(conn, project="p", name="other", value=9)
    db.insert_metric(conn, project="q", name="ctr", value=7)
    rows = db.get_metrics(conn, "p", "ctr")
    assert [r["value"] for r in rows] == [1.5, 2.0] and rows[0]["unit"] == "pct" and rows[0]["source"] == "demo"
    assert rows[1]["source"] == "record-metric" and rows[1]["unit"] is None
    assert [r["id"] for r in db.get_metrics(conn, "p", "ctr", before="2026-09-11 01:00:00")] == [a]
    assert [r["id"] for r in db.get_metrics(conn, "p", "ctr", after="2026-09-11 01:00:00")] == [b]


def test_metrics_are_append_only(conn):
    mid = db.insert_metric(conn, project="p", name="ctr", value=1)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE metrics SET value=2 WHERE id=?", (mid,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM metrics WHERE id=?", (mid,))


@pytest.mark.parametrize("bad", ["12", None, True, float("nan"), float("inf"), [1]])
def test_metric_value_must_be_a_finite_number(conn, bad):
    with pytest.raises((ValueError, TypeError, sqlite3.IntegrityError)):
        db.insert_metric(conn, project="p", name="ctr", value=bad)
    assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 0
