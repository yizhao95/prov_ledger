"""drift.detect_drift with declared drift kinds (extensions) — one test per op,
priority order, enabled=false, built-ins untouched."""
import json

from orchestrator import drift, extensions as ext


def _p(col, dtype="float64", null_frac=0.0, distinct=5, rows=10, dataset="ds"):
    return {"dataset": dataset, "column_name": col, "dtype": dtype,
            "null_frac": null_frac, "distinct_count": distinct, "row_count": rows}


def _ext(tmp_path, *kinds):
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"version": 1, "drift_kinds": list(kinds)}))
    return ext.load(str(p))


def _hits(drifts, col, kind):
    return [d for d in drifts if d["column"] == col and d["kind"] == kind]


def test_delta_gte_and_lte(tmp_path):
    e = _ext(tmp_path, {"id": "acme.null_up", "metric": "null_frac", "op": "delta_gte", "value": 0.1},
             {"id": "acme.rows_down", "metric": "row_count", "op": "delta_lte", "value": -5})
    d = drift.detect_drift([_p("a", null_frac=0.0, rows=10)], [_p("a", null_frac=0.15, rows=4)], extensions=e)
    h = _hits(d, "a", "acme.null_up")
    assert h and h[0]["before"] == 0.0 and h[0]["after"] == 0.15 and h[0]["source"] == "extension:acme.null_up"
    assert _hits(d, "a", "acme.rows_down")
    d2 = drift.detect_drift([_p("a", null_frac=0.0)], [_p("a", null_frac=0.05)], extensions=e)
    assert not _hits(d2, "a", "acme.null_up")


def test_eq_changed_became(tmp_path):
    e = _ext(tmp_path, {"id": "acme.is_object", "metric": "dtype", "op": "eq", "value": "object"},
             {"id": "acme.dtype_moved", "metric": "dtype", "op": "changed", "value": None},
             {"id": "acme.turned_object", "metric": "dtype", "op": "became", "value": "object"})
    d = drift.detect_drift([_p("a", dtype="int64")], [_p("a", dtype="object")], extensions=e)
    assert _hits(d, "a", "acme.is_object") and _hits(d, "a", "acme.dtype_moved") and _hits(d, "a", "acme.turned_object")
    d2 = drift.detect_drift([_p("a", dtype="object")], [_p("a", dtype="object")], extensions=e)
    assert _hits(d2, "a", "acme.is_object") and not _hits(d2, "a", "acme.dtype_moved") and not _hits(d2, "a", "acme.turned_object")


def test_dropped_below_and_rose_above_are_ratios(tmp_path):
    e = _ext(tmp_path, {"id": "acme.rows_halved", "metric": "row_count", "op": "dropped_below", "value": 0.5},
             {"id": "acme.distinct_doubled", "metric": "distinct_count", "op": "rose_above", "value": 2.0})
    d = drift.detect_drift([_p("a", rows=100, distinct=3)], [_p("a", rows=40, distinct=9)], extensions=e)
    assert _hits(d, "a", "acme.rows_halved")[0]["after"] == 40 and _hits(d, "a", "acme.distinct_doubled")
    # before == 0 / None: skipped, never a ZeroDivision
    d0 = drift.detect_drift([_p("a", rows=0, distinct=None)], [_p("a", rows=40, distinct=9)], extensions=e)
    assert not _hits(d0, "a", "acme.rows_halved") and not _hits(d0, "a", "acme.distinct_doubled")


def test_priority_orders_and_each_kind_fires_once_per_column(tmp_path):
    e = _ext(tmp_path, {"id": "acme.low", "metric": "null_frac", "op": "delta_gte", "value": 0.1, "priority": 1},
             {"id": "acme.high", "metric": "null_frac", "op": "delta_gte", "value": 0.1, "priority": 9})
    d = drift.detect_drift([_p("a"), _p("b")], [_p("a", null_frac=0.5), _p("b", null_frac=0.5)], extensions=e)
    ext_rows = [x for x in d if x["source"].startswith("extension:")]
    assert [x["kind"] for x in ext_rows] == ["acme.high", "acme.high", "acme.low", "acme.low"]
    assert len(_hits(d, "a", "acme.high")) == 1


def test_disabled_kind_never_fires(tmp_path):
    e = _ext(tmp_path, {"id": "acme.off", "metric": "null_frac", "op": "delta_gte", "value": 0.1, "enabled": False})
    d = drift.detect_drift([_p("a")], [_p("a", null_frac=0.9)], extensions=e)
    assert not _hits(d, "a", "acme.off") and _hits(d, "a", "null_spike")   # built-in still fires


def test_builtins_unchanged_without_extensions():
    prev, curr = [_p("a", dtype="int64"), _p("b")], [_p("a", dtype="object"), _p("c")]
    assert drift.detect_drift(prev, curr) == drift.detect_drift(prev, curr, extensions=ext.EMPTY)
    kinds = {d["kind"] for d in drift.detect_drift(prev, curr)}
    assert kinds == {"dtype_changed", "column_added", "column_dropped"}
    assert all("source" not in d or d["source"] == "builtin" for d in drift.detect_drift(prev, curr))


def test_extension_rows_carry_source_and_builtin_rows_do_not_change_shape(tmp_path):
    e = _ext(tmp_path, {"id": "acme.x", "metric": "null_frac", "op": "delta_gte", "value": 0.1})
    d = drift.detect_drift([_p("a")], [_p("a", null_frac=0.9)], extensions=e)
    b = next(x for x in d if x["kind"] == "null_spike")
    assert set(b) >= {"dataset", "column", "kind", "before", "after"}
    x = next(x for x in d if x["kind"] == "acme.x")
    assert x["source"] == "extension:acme.x" and x["dataset"] == "ds"
