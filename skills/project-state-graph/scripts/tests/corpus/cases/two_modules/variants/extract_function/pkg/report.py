"""Consumer of both readers."""
import pandas as pd

from pkg.sources import read_orders, read_customers


def join_customers(orders: pd.DataFrame, customers: pd.DataFrame) -> pd.DataFrame:
    return orders.merge(customers, on="customer_id")


def build_report(orders_path: str, customers_path: str) -> pd.DataFrame:
    orders = read_orders(orders_path)
    customers = read_customers(customers_path)
    df = join_customers(orders, customers)
    return df.groupby("region").amount.sum().reset_index()


def main():
    df = build_report("data/orders.csv", "data/customers.parquet")
    return df
