"""Baseline plus two body-identical helpers."""
import pandas as pd
from sklearn.model_selection import train_test_split

TEST_SIZE = 0.2


def load(path: str) -> pd.DataFrame:
    # read
    df = pd.read_parquet(path)
    return df


def clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df.qty > 0]
    df = df.dropna(subset=["label"])
    return df


def scale_x(df: pd.DataFrame) -> pd.DataFrame:
    return df.assign(v=df.qty * 2)


def scale_y(df: pd.DataFrame) -> pd.DataFrame:
    return df.assign(v=df.qty * 2)


def main():
    df = load("data/sales.parquet")
    df = clean(df)
    df = scale_x(df)
    df = scale_y(df)
    X_train, X_test = train_test_split(df, test_size=TEST_SIZE)
    return X_train, X_test
