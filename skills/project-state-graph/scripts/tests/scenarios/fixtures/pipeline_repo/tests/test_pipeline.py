import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pkg.pipeline import main  # noqa: E402
from pkg.report import summary  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_main_runs():
    model = main(os.path.join(ROOT, "data", "sales.csv"))
    assert "mean_revenue" in model


def test_summary_counts_rows():
    assert summary(os.path.join(ROOT, "data", "sales.csv"))["rows"] > 0
