"""The rollup's own unit tests — the green lights that bless the bug.

Every test here passes in BOTH worlds (healthy feed and drifted feed).
That's the demo's point: `pytest` green is a statement about the code,
not about the data the code is currently being fed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from revenue_rollup import delta_vs_baseline, rollup  # noqa: E402


def order(qty=1, unit_price=10.0, **extra):
    return {"order_id": 1, "ts": "2026-07-14", "store_id": "store-01",
            "sku": "sku-000", "qty": qty, "unit_price": unit_price, **extra}


def test_gross_is_qty_times_price():
    r = rollup([order(qty=3, unit_price=10.0, promo_discount=0.0)])
    assert r["gross"] == 30.0


def test_net_subtracts_promo_discount():
    r = rollup([order(qty=2, unit_price=20.0, promo_discount=5.0)])
    assert r["mean_net"] == 35.0


def test_missing_promo_discount_defaults_to_zero():
    # This test is the bug's alibi: it pins the .get(..., 0.0) behaviour as
    # CORRECT — which it is, per-record. A whole feed without the column is
    # a contract problem, invisible from inside this unit.
    r = rollup([order(qty=1, unit_price=10.0)])  # no promo_discount key
    assert r["mean_net"] == 10.0


def test_empty_feed_is_zero_not_crash():
    r = rollup([])
    assert r == {"orders": 0, "gross": 0, "discount_total": 0.0, "mean_net": 0.0}


def test_delta_vs_baseline_pct():
    assert delta_vs_baseline(51.6, {"mean_net_revenue": 43.0}) == 20.0


def test_delta_without_baseline_is_none():
    assert delta_vs_baseline(51.6, None) is None
