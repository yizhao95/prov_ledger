"""One reader feeding the report."""
import pandas as pd


def read_orders(path: str) -> pd.DataFrame:
    # keep paid orders only
    df = pd.read_csv(path)
    return df[df.amount > 0]
