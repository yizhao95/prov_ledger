"""CLI: render a project state-graph DB to an interactive HTML visualization.

Usage:
    python visualize.py <db_path> <out_path> [--level {subsystems,functions,full}]
                        [--title NAME] [--data-vars]

Levels:
    subsystems  high-level 2-level-dir dependency map
    functions   mid-level app functions wired by calls + data flow (variable
                passing); add --data-vars to include data_var nodes
    full        every node and edge with type-filter UI (default)
"""
from __future__ import annotations

import argparse
import os
import sys

# Allow running as a loose script (python visualize.py ...) or as a module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import graph_viz  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Visualize a project state graph.")
    p.add_argument("db_path", help="path to <project>-state-graph.db")
    p.add_argument("out_path", help="output .html file")
    p.add_argument("--level", choices=["subsystems", "functions", "full"],
                   default="full")
    p.add_argument("--title", default=None, help="title shown in the header")
    p.add_argument("--data-vars", action="store_true",
                   help="(functions level) include data_var nodes")
    args = p.parse_args(argv)

    title = args.title or os.path.basename(args.db_path).replace(
        "-state-graph.db", "")
    builder = graph_viz._BUILDERS[args.level]
    data = builder(args.db_path, args.data_vars)
    graph_viz.write_html(args.db_path, args.out_path, level=args.level,
                         title=title, include_data_vars=args.data_vars)
    print(f"wrote {args.out_path} [{args.level}] "
          f"nodes={len(data['nodes'])} edges={len(data['edges'])} "
          f"size={round(os.path.getsize(args.out_path) / 1024, 1)}KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
