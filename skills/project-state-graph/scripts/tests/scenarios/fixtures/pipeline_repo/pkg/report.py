"""Consumer of clean(): a row count used by the weekly report."""
from pkg.pipeline import clean, load


def summary(path: str = "data/sales.csv") -> dict:
    rows = clean(load(path))
    return {"rows": len(rows), "labels": sorted({r["label"] for r in rows})}
