"""CLI: render a project state-graph DB to the four provLedger Phase B slices.

Usage:
    python viz_slices.py <db_path> <out_path> [--title NAME] [--focus QNAME]

Emits ONE self-contained HTML file (file://-safe) with four tabs:
  ① Dataflow + datatype   ② Function call chain   ③ Pipeline   ④ API surface
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analyzer import slice_viz  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Render the 4 provLedger Phase B slices.")
    p.add_argument("db_path", help="path to <project>-state-graph.db")
    p.add_argument("out_path", help="output .html file")
    p.add_argument("--title", default=None, help="title shown in the header")
    p.add_argument("--focus", default=None,
                   help="default focus function (qualified_name) for the call chain")
    args = p.parse_args(argv)

    title = args.title or os.path.basename(args.db_path).replace(
        "-state-graph.db", "")

    dataflow = slice_viz.build_dataflow(args.db_path)
    pipeline = slice_viz.build_pipeline(args.db_path)
    api = slice_viz.build_api_surface(args.db_path)
    cov = dataflow["dtype_coverage"]

    slice_viz.write_slices(args.db_path, args.out_path, title=title)
    # honor an explicit --focus by patching the default after render
    if args.focus:
        _inject_focus(args.out_path, args.focus)

    print(
        f"wrote {args.out_path}  "
        f"[dataflow nodes={len(dataflow['nodes'])} edges={len(dataflow['edges'])}] "
        f"[pipeline {len(pipeline['pipelines'])}] "
        f"[api rows={len(api)}] "
        f"dtype coverage: {cov['pct']}% "
        f"({cov['typed']} typed / {cov['unknown']} unknown)  "
        f"size={round(os.path.getsize(args.out_path) / 1024, 1)}KB"
    )
    return 0


def _inject_focus(out_path: str, focus: str) -> None:
    """Replace the auto-picked call-chain default with an explicit --focus."""
    import json
    import re
    html = open(out_path, encoding="utf-8").read()
    html = re.sub(r'"callchain_default":\s*("(?:[^"\\]|\\.)*"|null)',
                  '"callchain_default": ' + json.dumps(focus), html, count=1)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)


if __name__ == "__main__":
    raise SystemExit(main())
