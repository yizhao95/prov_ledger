"""Phase 1.2 — drift detection over data_profile snapshots."""
from __future__ import annotations

from orchestrator import drift


def _p(col, dtype, null_frac=0.0, distinct=5, rows=10, dataset="ds"):
    return {"dataset": dataset, "column_name": col, "dtype": dtype,
            "null_frac": null_frac, "distinct_count": distinct, "row_count": rows}


def _kinds(drifts, col):
    return {d["kind"] for d in drifts if d["column"] == col}


def test_dtype_changed():
    prev = [_p("label", "int64")]
    curr = [_p("label", "object")]  # numeric -> string
    d = drift.detect_drift(prev, curr)
    assert "dtype_changed" in _kinds(d, "label")
    got = next(x for x in d if x["kind"] == "dtype_changed")
    assert got["before"] == "int64" and got["after"] == "object"


def test_null_spike():
    prev = [_p("f", "float64", null_frac=0.0)]
    curr = [_p("f", "float64", null_frac=0.8)]
    assert "null_spike" in _kinds(drift.detect_drift(prev, curr), "f")


def test_cardinality_collapse():
    prev = [_p("z", "int64", distinct=7, rows=10)]
    curr = [_p("z", "int64", distinct=1, rows=10)]  # constant / all-zero class
    assert "cardinality_collapse" in _kinds(drift.detect_drift(prev, curr), "z")


def test_column_added_and_dropped():
    prev = [_p("a", "int64")]
    curr = [_p("b", "int64")]
    d = drift.detect_drift(prev, curr)
    assert "column_added" in _kinds(d, "b")
    assert "column_dropped" in _kinds(d, "a")


def test_no_drift_when_stable():
    prev = [_p("a", "int64", null_frac=0.1, distinct=5)]
    curr = [_p("a", "int64", null_frac=0.1, distinct=5)]
    assert drift.detect_drift(prev, curr) == []
