#!/usr/bin/env python3
"""Deterministic synthetic upstream for the phantom-uplift demo (seed 42).

Simulates a checkout service that publishes one JSON record per order (the
shape of a BigQuery events export): ids, timestamp, store, sku, qty,
unit_price, and `promo_discount` — the dollars taken off this order by the
promo that is running this week (0.0 when the order wasn't in the promo).

Emits three files next to this script:
  orders_fixed.json       — the healthy feed: every order carries promo_discount
  orders_drifted.json     — the SAME orders after the checkout-service v2
                            rollout silently dropped the field (the whole demo)
  last_week_metrics.json  — last week's rollup baseline (same promo running);
                            "+X% week-over-week" is measured against this

Stdlib-only and fully deterministic: same seed -> byte-identical output.
This data is synthetic and illustrative; the failure mechanism it reproduces
(an upstream column silently disappearing, downstream code imputing a default)
is a reconstruction of a real-world failure *class*, not of any real incident.

Honest-numbers note: PROMO_ATTACH_RATE and PROMO_DEPTH are the two knobs that
set the size of the phantom uplift (E[uplift] ≈ 1/(1 - attach·depth) - 1,
≈ +24% with the values below). They are declared here, not hidden — the demo
reports whatever the generated data actually computes to.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

SEED_THIS_WEEK = 42
SEED_LAST_WEEK = 41
N_ORDERS = 1200
HERE = Path(__file__).resolve().parent

PROMO_ATTACH_RATE = 0.50    # fraction of orders in the promo
PROMO_DEPTH = (0.22, 0.42)  # discount as a fraction of order gross, uniform

THIS_WEEK_DAYS = [f"2026-07-{d:02d}" for d in range(13, 20)]
LAST_WEEK_DAYS = [f"2026-07-{d:02d}" for d in range(6, 13)]

# A small SKU catalog whose price mix lands mean gross/order near $50.
CATALOG = [(f"sku-{i:03d}", price) for i, price in enumerate(
    [3.99, 4.49, 5.99, 6.49, 7.99, 8.99, 9.99, 11.49, 12.99, 13.99,
     14.49, 15.99, 17.99, 18.49, 19.99, 21.99, 22.49, 24.99, 26.99, 28.99,
     29.99, 31.49, 33.99, 35.99, 38.49, 39.99, 42.99, 44.99, 47.99, 49.99,
     52.99, 55.99, 59.99, 64.99, 69.99, 74.99, 79.99, 84.99, 89.99, 99.99])]
QTY_CHOICES, QTY_WEIGHTS = [1, 2, 3, 4], [0.55, 0.25, 0.13, 0.07]


def generate(rng: random.Random, days: list[str], id_base: int) -> list[dict]:
    """One week of checkout orders, every record carrying promo_discount."""
    orders = []
    for i in range(N_ORDERS):
        sku, unit_price = rng.choice(CATALOG)
        qty = rng.choices(QTY_CHOICES, weights=QTY_WEIGHTS)[0]
        gross = qty * unit_price
        discount = 0.0
        if rng.random() < PROMO_ATTACH_RATE:
            discount = round(gross * rng.uniform(*PROMO_DEPTH), 2)
        orders.append({
            "order_id": id_base + i,
            "ts": rng.choice(days),
            "store_id": f"store-{rng.randint(1, 8):02d}",
            "sku": sku,
            "qty": qty,
            "unit_price": unit_price,
            "promo_discount": discount,
        })
    return orders


def mean_net(orders: list[dict]) -> float:
    total = sum(o["qty"] * o["unit_price"] - o["promo_discount"] for o in orders)
    return round(total / len(orders), 2)


def main() -> None:
    this_week = generate(random.Random(SEED_THIS_WEEK), THIS_WEEK_DAYS, 100_000)
    last_week = generate(random.Random(SEED_LAST_WEEK), LAST_WEEK_DAYS, 90_000)

    (HERE / "orders_fixed.json").write_text(
        json.dumps(this_week, indent=1) + "\n", encoding="utf-8")

    drifted = [{k: v for k, v in o.items() if k != "promo_discount"}
               for o in this_week]
    (HERE / "orders_drifted.json").write_text(
        json.dumps(drifted, indent=1) + "\n", encoding="utf-8")

    baseline = {"week_of": LAST_WEEK_DAYS[0], "orders": len(last_week),
                "mean_net_revenue": mean_net(last_week)}
    (HERE / "last_week_metrics.json").write_text(
        json.dumps(baseline, indent=1) + "\n", encoding="utf-8")

    print(f"wrote {len(this_week)} orders -> orders_fixed.json, "
          f"orders_drifted.json (promo_discount dropped), "
          f"last_week_metrics.json (baseline ${baseline['mean_net_revenue']:.2f})")


if __name__ == "__main__":
    main()
