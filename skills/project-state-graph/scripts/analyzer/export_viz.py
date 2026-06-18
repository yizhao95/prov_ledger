"""Backward-compatible shim over :mod:`analyzer.graph_viz`.

Historically this module owned the interactive HTML export. That logic now lives
in ``graph_viz`` (which supports multiple levels). This module keeps the original
public API — ``export_graph`` and ``write_html`` — so existing callers and tests
keep working, delegating to the ``full`` level of ``graph_viz``.
"""
from __future__ import annotations

from typing import Dict

from analyzer import graph_viz


def export_graph(db_path: str) -> Dict:
    """Return the full graph dict (nodes/edges/node_types/edge_types)."""
    return graph_viz.build_full(db_path)


def write_html(db_path: str, out_path: str, *, title: str = "state-graph") -> str:
    """Write the self-contained full-graph interactive viewer to out_path."""
    return graph_viz.write_html(db_path, out_path, level="full", title=title)
