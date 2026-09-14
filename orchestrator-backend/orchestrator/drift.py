"""Drift detection over data_profile snapshots (Phase 1.2).

Compares a previous profile to the current one (lists of profile-row dicts as
produced by orchestrator.profiler / stored in data_profile) and returns the
silent-failure-relevant changes. Pure stdlib.

Drift kinds:
  dtype_changed        - a column's dtype changed (e.g. numeric -> string)
  null_spike           - null fraction jumped by >= null_spike_delta
  cardinality_collapse - column became constant (distinct==1, >1 rows) — the
                         all-zero / degenerate class
  column_added / column_dropped
"""
from __future__ import annotations

from typing import Optional


def _drift(dataset, column, kind, before, after, source: str = "builtin") -> dict:
    """One drift row. `source` says where the kind came from: "builtin" or
    "extension:<id>" (a declared kind from provledger-extensions.json)."""
    return {"dataset": dataset, "column": column, "kind": kind,
            "before": before, "after": after, "source": source}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _declared_hit(op: str, value, before, after) -> bool:
    """One declared drift kind's predicate over a column's before/after metric."""
    if op == "changed":
        return before is not None and after is not None and before != after
    if op == "eq":
        return after is not None and after == value
    if op == "became":
        return after is not None and after == value and before != value
    b, a = _num(before), _num(after)
    if op in ("delta_gte", "delta_lte"):
        v = _num(value)
        if b is None or a is None or v is None:
            return False
        return (a - b >= v) if op == "delta_gte" else (a - b <= v)
    if op in ("dropped_below", "rose_above"):
        v = _num(value)
        if b is None or a is None or v is None or b == 0:
            return False
        ratio = a / b
        return (ratio < v) if op == "dropped_below" else (ratio > v)
    return False


def _declared_drifts(prevm: dict, currm: dict, extensions) -> list[dict]:
    """Declared kinds after the built-ins: by priority (larger first, then id),
    one row per column per kind, source="extension:<id>" (observed tier — the
    predicate is deterministic)."""
    out: list[dict] = []
    kinds = sorted((k for k in getattr(extensions, "drift_kinds", ()) if k.enabled), key=lambda k: (-k.priority, k.id))
    for k in kinds:
        for col, c in currm.items():
            p = prevm.get(col)
            if p is None:
                continue
            before, after = p.get(k.metric), c.get(k.metric)
            if _declared_hit(k.op, k.value, before, after):
                out.append(_drift(c.get("dataset"), col, k.id, before, after, source=f"extension:{k.id}"))
    return out


def detect_drift(
    prev: list[dict], curr: list[dict], *,
    declared_schema: Optional[dict] = None,
    null_spike_delta: float = 0.3,
    extensions=None,
) -> list[dict]:
    """Return the list of drifts from `prev` to `curr` profile rows.

    `declared_schema` (optional) maps column -> expected dtype; when given, a
    current dtype differing from the declared type is also reported.
    `extensions` (optional, an extensions.Extensions) adds the declared drift
    kinds after the built-ins — see orchestrator.extensions. This function is
    pure: it never discovers the extensions file itself.
    """
    prevm = {r["column_name"]: r for r in prev}
    currm = {r["column_name"]: r for r in curr}
    out: list[dict] = []

    for col, c in currm.items():
        ds = c.get("dataset")
        p = prevm.get(col)
        c_dtype = c.get("dtype")

        if p is None:
            out.append(_drift(ds, col, "column_added", None, c_dtype))
        else:
            p_dtype = p.get("dtype")
            if p_dtype and c_dtype and p_dtype != c_dtype:
                out.append(_drift(ds, col, "dtype_changed", p_dtype, c_dtype))
            pf = p.get("null_frac") or 0.0
            cf = c.get("null_frac") or 0.0
            if cf - pf >= null_spike_delta:
                out.append(_drift(ds, col, "null_spike", pf, cf))

        if declared_schema and col in declared_schema:
            want = declared_schema[col]
            if c_dtype and want and c_dtype != want:
                out.append(_drift(ds, col, "dtype_vs_declared", want, c_dtype))

        # cardinality collapse is a current-state signal: fire when the column is
        # now constant and it either wasn't before or there is no prior profile.
        if (c.get("distinct_count") == 1 and (c.get("row_count") or 0) > 1
                and (p is None or p.get("distinct_count") != 1)):
            before = p.get("distinct_count") if p else None
            out.append(_drift(ds, col, "cardinality_collapse", before, 1))

    for col, p in prevm.items():
        if col not in currm:
            out.append(_drift(p.get("dataset"), col, "column_dropped",
                              p.get("dtype"), None))

    if extensions is not None:
        out.extend(_declared_drifts(prevm, currm, extensions))
    return out
