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


def _drift(dataset, column, kind, before, after) -> dict:
    return {"dataset": dataset, "column": column, "kind": kind,
            "before": before, "after": after}


def detect_drift(
    prev: list[dict], curr: list[dict], *,
    declared_schema: Optional[dict] = None,
    null_spike_delta: float = 0.3,
) -> list[dict]:
    """Return the list of drifts from `prev` to `curr` profile rows.

    `declared_schema` (optional) maps column -> expected dtype; when given, a
    current dtype differing from the declared type is also reported.
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

    return out
