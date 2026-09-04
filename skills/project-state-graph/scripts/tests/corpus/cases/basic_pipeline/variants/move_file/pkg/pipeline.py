"""Baseline: load -> clean -> split -> fit."""
from sklearn.model_selection import train_test_split

from pkg.io_utils import load, clean

TEST_SIZE = 0.2


def main():
    df = load("data/sales.parquet")
    df = clean(df)
    X_train, X_test = train_test_split(df, test_size=TEST_SIZE)
    return X_train, X_test
