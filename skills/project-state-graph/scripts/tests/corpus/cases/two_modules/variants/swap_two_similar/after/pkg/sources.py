"""Two readers plus two body-identical json parsers."""
import pandas as pd


def read_orders(path: str) -> pd.DataFrame:
    # keep paid orders only
    df = pd.read_csv(path)
    return df[df.amount > 0]


def read_customers(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    return df.drop_duplicates(subset=["customer_id"])


def load_x(path: str) -> pd.DataFrame:
    return pd.read_json(path, lines=True)


def load_y(path: str) -> pd.DataFrame:
    return pd.read_json(path, lines=True)
