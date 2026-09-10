"""Consumer of both readers."""
import pandas as pd

from pkg.inputs import read_orders, read_customers


def build_report(orders_path: str, customers_path: str) -> pd.DataFrame:
    orders = read_orders(orders_path)
    customers = read_customers(customers_path)
    df = orders.merge(customers, on="customer_id")
    return df.groupby("region").amount.sum().reset_index()


def main():
    df = build_report("data/orders.csv", "data/customers.parquet")
    return df


def entry():
    return main()
