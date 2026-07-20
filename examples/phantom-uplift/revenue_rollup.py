#!/usr/bin/env python3
"""Downstream job: the weekly promo-revenue rollup for the merch dashboard.

This is ordinary business code — it knows nothing about provLedger. It reads
the checkout-orders feed, computes net revenue per order, compares against
last week's baseline, and exits 0.

The point of the demo: when the upstream silently drops `promo_discount`,
this job STILL runs green. `order.get("promo_discount", 0.0)` imputes $0 for
every order, so net revenue = gross revenue, and the rollup reports a big
week-over-week uplift — a number everyone is happy to accept. Nothing here
raises; the catch has to come from the data contract layer around it.

Usage: revenue_rollup.py <orders.json> [<last_week_metrics.json>]
Prints one machine-readable line:
  RESULT mean_net=<float> discount_total=<float> delta_pct=<float|na>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def rollup(orders: list[dict]) -> dict:
    """Gross / discount / net totals for one week of orders."""
    gross = sum(o["qty"] * o["unit_price"] for o in orders)
    # The line the whole demo is about: when the upstream feed stops sending
    # promo_discount, this silently turns every discount into $0.
    discounts = sum(float(o.get("promo_discount", 0.0)) for o in orders)
    net = gross - discounts
    return {
        "orders": len(orders),
        "gross": round(gross, 2),
        "discount_total": round(discounts, 2),
        "mean_net": round(net / len(orders), 2) if orders else 0.0,
    }


def delta_vs_baseline(mean_net: float, baseline: dict | None) -> float | None:
    """Week-over-week change in mean net revenue per order, in percent."""
    if not baseline or not baseline.get("mean_net_revenue"):
        return None
    prev = baseline["mean_net_revenue"]
    return round(100.0 * (mean_net - prev) / prev, 1)


def main() -> int:
    orders_path = Path(sys.argv[1])
    baseline = (json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
                if len(sys.argv) > 2 else None)
    orders = json.loads(orders_path.read_text(encoding="utf-8"))

    r = rollup(orders)
    delta = delta_vs_baseline(r["mean_net"], baseline)

    print(f"[rollup] {r['orders']} orders from {orders_path.name}")
    print(f"[rollup] gross revenue: ${r['gross']:,.2f} · "
          f"discounts applied: ${r['discount_total']:,.2f}")
    wow = f"  ({delta:+.1f}% vs last week)" if delta is not None else ""
    print(f"[rollup] mean net revenue / order: ${r['mean_net']:.2f}{wow}")
    print("[rollup] exit_code=0")
    print(f"RESULT mean_net={r['mean_net']:.2f} "
          f"discount_total={r['discount_total']:.2f} "
          f"delta_pct={f'{delta:.1f}' if delta is not None else 'na'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
