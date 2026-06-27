"""Phase 1.1 — data_profile schema, profiler (stdlib + optional pandas), recorder."""
from __future__ import annotations

import sqlite3

import pytest

from orchestrator import db, profiler


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_012_data_profile_schema(conn: sqlite3.Connection) -> None:
    assert _cols(conn, "data_profile") >= {
        "project", "plan_id", "step_id", "dataset", "column_name",
        "dtype", "null_frac", "row_count", "distinct_count", "observed_at"}
    idx = {r[1] for r in conn.execute("PRAGMA index_list(data_profile)").fetchall()}
    assert "idx_data_profile_ds" in idx


def test_profile_records_stdlib_path() -> None:
    # No pandas needed: profile a list of plain dict rows.
    rows = [{"x": 1, "y": None, "k": 7},
            {"x": 2, "y": None, "k": 7},
            {"x": 3, "y": 5, "k": 7}]
    out = {r["column_name"]: r for r in profiler.profile_records(rows, dataset="ds")}
    assert out["x"]["dtype"] == "int" and out["x"]["null_frac"] == 0.0
    assert out["y"]["null_frac"] == pytest.approx(2 / 3)
    assert out["k"]["distinct_count"] == 1          # constant column (collapse signal)
    assert out["x"]["row_count"] == 3
    assert out["x"]["dataset"] == "ds"


def test_profile_dataframe_path() -> None:
    pd = pytest.importorskip("pandas")  # optional; skipped if pandas absent
    df = pd.DataFrame({"n": [1.0, 2.0, 3.0], "z": [0, 0, 0], "s": ["a", None, "b"]})
    out = {r["column_name"]: r for r in profiler.profile_dataframe(df, dataset="d2")}
    assert "float" in out["n"]["dtype"]
    assert out["z"]["distinct_count"] == 1
    assert out["s"]["null_frac"] == pytest.approx(1 / 3)
    assert out["n"]["row_count"] == 3


def test_recorder_roundtrip(conn: sqlite3.Connection) -> None:
    rows = profiler.profile_records(
        [{"a": 1}, {"a": 2}], dataset="sales", project="proj", plan_id="p1", step_id="s1")
    db.insert_data_profile(conn, rows)
    got = db.get_data_profile(conn, "sales", project="proj")
    assert len(got) == 1
    assert got[0]["column_name"] == "a" and got[0]["project"] == "proj"
    assert got[0]["row_count"] == 2
