"""Consumer of both readers."""
from pkg.builder import build_report


def main():
    df = build_report("data/orders.csv", "data/customers.parquet")
    return df
