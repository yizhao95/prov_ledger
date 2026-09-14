"""Baseline pipeline: load -> clean -> split -> fit."""
import csv

TEST_SIZE = 0.2
MIN_QTY = 0


def load(path: str) -> list:
    # read the rows as dicts with numeric qty / price
    with open(path, newline="") as fh:
        df = [dict(r) for r in csv.DictReader(fh)]
    for r in df:
        r["qty"] = int(r["qty"])
        r["price"] = float(r["price"])
    return df


def clean(df: list) -> list:
    df = [r for r in df if r["qty"] > MIN_QTY]
    df = [r for r in df if r.get("label")]
    return df


def split(df: list, test_size: float = TEST_SIZE) -> tuple:
    cut = int(len(df) * (1 - test_size))
    X_train = df[:cut]
    X_test = df[cut:]
    return X_train, X_test


def fit(X_train: list) -> dict:
    total = sum(r["qty"] * r["price"] for r in X_train)
    return {"mean_revenue": total / max(len(X_train), 1)}


def main(path: str = "data/sales.csv") -> dict:
    df = load(path)
    df = clean(df)
    X_train, X_test = split(df)
    model = fit(X_train)
    model["n_test"] = len(X_test)
    return model
