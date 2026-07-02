#!/usr/bin/env python3
"""Downstream job: segment detection events into 6 category clusters.

This is ordinary business code — it knows nothing about provLedger. It builds
features from whatever columns the upstream feed happens to contain, clusters
them with KMeans, and reports purity against the manual-audit ground truth.

The point of the demo: when the upstream silently drops the `class` enrichment,
this job STILL runs green ("6 clusters formed, exit_code=0") — only the purity
quietly collapses. Nothing here raises; the catch has to come from the data
contract layer around it.

Usage: cluster_and_eval.py <events.json> <ground_truth.csv>
Prints one machine-readable line: RESULT purity=<float> enrichment=<yes|no>
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from sklearn.cluster import KMeans

SEED = 42
N_CLUSTERS = 6
CLASS_WEIGHT = 4.0  # how strongly the class enrichment separates the segments


def build_features(events: list[dict]) -> tuple[np.ndarray, bool]:
    """Geometry features, plus a weighted one-hot of `class` when present."""
    geom = np.array([
        [e["xmax"] - e["xmin"],
         e["ymax"] - e["ymin"],
         (e["xmax"] - e["xmin"]) * (e["ymax"] - e["ymin"]),
         (e["xmax"] - e["xmin"]) / max(e["ymax"] - e["ymin"], 1e-6),
         (e["xmin"] + e["xmax"]) / 2,
         (e["ymin"] + e["ymax"]) / 2]
        for e in events
    ])
    geom = (geom - geom.mean(axis=0)) / (geom.std(axis=0) + 1e-9)

    has_class = all("class" in e for e in events) and len(events) > 0
    if not has_class:
        return geom, False

    names = sorted({e["class"] for e in events})
    onehot = np.zeros((len(events), len(names)))
    for i, e in enumerate(events):
        onehot[i, names.index(e["class"])] = CLASS_WEIGHT
    return np.hstack([geom, onehot]), True


def purity(labels: np.ndarray, truth: list[str]) -> float:
    """Sum over clusters of the majority true class, over N."""
    total = 0
    for c in set(labels):
        members = [truth[i] for i in range(len(truth)) if labels[i] == c]
        total += Counter(members).most_common(1)[0][1]
    return total / len(truth)


def main() -> int:
    events_path, truth_path = Path(sys.argv[1]), Path(sys.argv[2])
    events = json.loads(events_path.read_text(encoding="utf-8"))
    with open(truth_path, encoding="utf-8") as f:
        truth_by_id = {int(r["box_id"]): r["class"] for r in csv.DictReader(f)}
    truth = [truth_by_id[e["box_id"]] for e in events]

    features, enriched = build_features(events)
    print(f"[cluster] {len(events)} events from {events_path.name} "
          f"(class enrichment: {'present' if enriched else 'ABSENT'})")

    km = KMeans(n_clusters=N_CLUSTERS, random_state=SEED, n_init=10)
    labels = km.fit_predict(features)
    print(f"[cluster] {N_CLUSTERS} clusters formed. exit_code=0")

    p = purity(labels, truth)
    print(f"[eval] segment purity vs {truth_path.name}: {p:.2f}")
    print(f"RESULT purity={p:.2f} enrichment={'yes' if enriched else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
