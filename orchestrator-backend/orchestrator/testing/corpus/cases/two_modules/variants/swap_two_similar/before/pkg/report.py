"""Consumer of both readers and both parsers."""
import pandas as pd

from pkg.sources import read_orders, read_customers, parse_a, parse_b


def build_report(orders_path: str, customers_path: str) -> pd.DataFrame:
    orders = read_orders(orders_path)
    customers = read_customers(customers_path)
    df = orders.merge(customers, on="customer_id")
    return df.groupby("region").amount.sum().reset_index()


def main():
    df = build_report("data/orders.csv", "data/customers.parquet")
    extra = pd.concat([parse_a("data/a.jsonl"), parse_b("data/b.jsonl")])
    return df, extra
