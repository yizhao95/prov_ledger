"""Stage 1 — clean the raw events and split ONCE.

This file does the right thing: one temporal-ish cleanup, one
train/test split, both halves persisted. From here on, `train.parquet`
and `test.parquet` are two halves of ONE partition — every downstream
file is supposed to treat that as a contract.
"""
import pandas as pd
from sklearn.model_selection import train_test_split


def load_events(path: str) -> pd.DataFrame:
    return pd.read_csv(path, parse_dates=["ts"])


def clean(raw_df: pd.DataFrame) -> pd.DataFrame:
    df = raw_df.dropna(subset=["amount", "ts"])
    df = df[df["amount"].between(0, df["amount"].quantile(0.999))]
    return df.sort_values("ts").reset_index(drop=True)


def split(clean_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    train_df, test_df = train_test_split(
        clean_df, test_size=0.2, random_state=42, shuffle=False)
    return train_df, test_df


def main() -> None:
    raw_df = load_events("events.csv")
    clean_df = clean(raw_df)
    train_df, test_df = split(clean_df)
    train_df.to_parquet("train.parquet")
    test_df.to_parquet("test.parquet")


if __name__ == "__main__":
    main()
