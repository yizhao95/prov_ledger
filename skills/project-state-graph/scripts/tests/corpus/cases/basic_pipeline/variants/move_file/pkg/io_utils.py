"""I/O helpers moved out of the pipeline module."""
import pandas as pd


def load(path: str) -> pd.DataFrame:
    # read
    df = pd.read_parquet(path)
    return df


def clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df.qty > 0]
    df = df.dropna(subset=["label"])
    return df
