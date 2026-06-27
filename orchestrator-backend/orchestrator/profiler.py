"""Runtime data profiler (Phase 1.1).

Captures one snapshot row per column of a dataset: dtype, null fraction, row
count, distinct count. Two entry points:

  - profile_records(rows, ...) : stdlib-only, profiles a list of dict rows.
  - profile_dataframe(df, ...) : requires pandas (OPTIONAL — imported lazily so
                                 the orchestrator core stays stdlib-only).

Both return a list of dicts shaped for db.insert_data_profile().
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


def _infer_dtype(nonnull: list) -> str:
    """Best-effort Python-type name for a column's non-null values."""
    if not nonnull:
        return "unknown"
    types = {type(v) for v in nonnull}
    if len(types) == 1:
        return next(iter(types)).__name__
    # bool is a subclass of int — treat a bool/int mix as int, else "mixed".
    if types <= {bool, int}:
        return "int"
    if types <= {int, float, bool}:
        return "float"
    return "mixed"


def _row(dataset, project, plan_id, step_id, column, dtype, null_frac,
         row_count, distinct_count) -> dict:
    return {
        "project": project, "plan_id": plan_id, "step_id": step_id,
        "dataset": dataset, "column_name": str(column), "dtype": dtype,
        "null_frac": null_frac, "row_count": row_count,
        "distinct_count": distinct_count,
    }


def profile_records(
    records: Iterable[dict], *, dataset: str, project: Optional[str] = None,
    plan_id: Optional[str] = None, step_id: Optional[str] = None,
) -> list[dict]:
    """Profile a list of dict rows (stdlib only)."""
    rows = list(records)
    n = len(rows)
    columns: dict[str, list] = {}
    for r in rows:
        for k, v in r.items():
            columns.setdefault(k, []).append(v)
    out: list[dict] = []
    for col, vals in columns.items():
        nonnull = [v for v in vals if v is not None]
        nulls = len(vals) - len(nonnull)
        try:
            distinct = len({v for v in nonnull})
        except TypeError:  # unhashable values — fall back to a repr set
            distinct = len({repr(v) for v in nonnull})
        out.append(_row(dataset, project, plan_id, step_id, col,
                        _infer_dtype(nonnull), (nulls / n) if n else 0.0,
                        n, distinct))
    return out


def profile_dataframe(
    df: Any, *, dataset: str, project: Optional[str] = None,
    plan_id: Optional[str] = None, step_id: Optional[str] = None,
) -> list[dict]:
    """Profile a pandas DataFrame. Requires pandas (imported lazily)."""
    try:
        import pandas  # noqa: F401
    except ImportError as e:  # pragma: no cover - exercised only without pandas
        raise RuntimeError(
            "profile_dataframe requires pandas; install it or use profile_records"
        ) from e
    n = int(len(df))
    out: list[dict] = []
    for col in df.columns:
        s = df[col]
        out.append(_row(
            dataset, project, plan_id, step_id, col,
            str(s.dtype),
            float(s.isna().mean()) if n else 0.0,
            n,
            int(s.nunique(dropna=True)),
        ))
    return out
