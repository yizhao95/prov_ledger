"""Baseline: load -> split -> fit (clean removed)."""
import pandas as pd
from sklearn.model_selection import train_test_split

TEST_SIZE = 0.2


def load(path: str) -> pd.DataFrame:
    # read
    df = pd.read_parquet(path)
    return df


def main():
    df = load("data/sales.parquet")
    X_train, X_test = train_test_split(df, test_size=TEST_SIZE)
    return X_train, X_test


def entry():
    return main()
