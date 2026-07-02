#!/usr/bin/env python3
"""Deterministic synthetic upstream for the silent-class-drop demo (seed 42).

Simulates a shelf-monitoring vision service that publishes detection events:
one JSON record per detected item box, enriched with a `class` field (the
product category, joined in from a catalog service).

Emits three files next to this script:
  upstream_fixed.json    — the healthy feed: every event carries `class`
  upstream_drifted.json  — the SAME events after an upstream refactor silently
                           dropped the `class` enrichment (the whole demo)
  ground_truth.csv       — box_id,class from a manual audit (evaluation only)

Stdlib-only and fully deterministic: same seed -> byte-identical output.
This data is synthetic and illustrative; the failure mechanism it reproduces
(an upstream column silently disappearing) is real-world-derived.
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

SEED = 42
N_EVENTS = 300
HERE = Path(__file__).resolve().parent

# Six product categories. Box geometry per category overlaps heavily on
# purpose: geometry alone is a weak signal, the class enrichment is the strong
# one — so silently losing `class` collapses the downstream segmentation while
# every process still exits 0.
CATEGORIES = [
    # (name, mean_w, mean_h)  — pixel sizes on a 1920x1080 shelf camera frame
    ("beverage",  90, 200),
    ("snack",    140, 180),
    ("dairy",    110, 160),
    ("produce",  150, 140),
    ("household", 170, 210),
    ("frozen",   120, 190),
]
GEOM_NOISE = 55.0   # std-dev of the size noise; large vs the ~30px class gaps
JOIN_ERROR = 0.10   # fraction of events whose catalog join tags the WRONG class


def generate(rng: random.Random) -> tuple[list[dict], list[str]]:
    """Returns (feed events, true classes). The feed's `class` comes from a
    catalog join that mislabels JOIN_ERROR of the events; ground_truth.csv is
    the manual audit, i.e. the true classes."""
    events, truth = [], []
    for i in range(N_EVENTS):
        name, mw, mh = CATEGORIES[i % len(CATEGORIES)]
        w = max(20.0, rng.gauss(mw, GEOM_NOISE))
        h = max(20.0, rng.gauss(mh, GEOM_NOISE))
        cx = rng.uniform(w / 2, 1920 - w / 2)
        cy = rng.uniform(h / 2, 1080 - h / 2)
        feed_class = name
        if rng.random() < JOIN_ERROR:
            feed_class = rng.choice([c for c, _, _ in CATEGORIES if c != name])
        events.append({
            "box_id": i,
            "xmin": round(cx - w / 2, 2),
            "xmax": round(cx + w / 2, 2),
            "ymin": round(cy - h / 2, 2),
            "ymax": round(cy + h / 2, 2),
            "class": feed_class,
        })
        truth.append(name)
    return events, truth


def main() -> None:
    events, truth = generate(random.Random(SEED))

    (HERE / "upstream_fixed.json").write_text(
        json.dumps(events, indent=1) + "\n", encoding="utf-8")

    drifted = [{k: v for k, v in e.items() if k != "class"} for e in events]
    (HERE / "upstream_drifted.json").write_text(
        json.dumps(drifted, indent=1) + "\n", encoding="utf-8")

    with open(HERE / "ground_truth.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["box_id", "class"])
        for e, true_class in zip(events, truth):
            w.writerow([e["box_id"], true_class])

    print(f"wrote {len(events)} events -> upstream_fixed.json, "
          f"upstream_drifted.json (class dropped), ground_truth.csv")


if __name__ == "__main__":
    main()
