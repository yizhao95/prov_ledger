"""Consumer of the orders reader."""
import pandas as pd

from pkg.sources import read_orders


def build_report(orders_path: str, customers_path: str) -> pd.DataFrame:
    orders = read_orders(orders_path)
    return orders.groupby("region").amount.sum().reset_index()


def main():
    df = build_report("data/orders.csv", "data/customers.parquet")
    return df
