#!/usr/bin/env python3
"""The deck act of the demo: a number in a deck, and what happens when the deck
is revised under it (DP phase 4, spec §9).

The phantom-uplift demo already shows a metric going wrong inside a pipeline.
This one follows the same number OUT of the pipeline and into the slide where
somebody actually reads it:

  1. two metrics land in the ledger — q3_conv 3.2 and net_revenue 64.33;
  2. the deck is anchored twice: `slide 4` carries the conversion figure,
     `slide 2` carries net revenue. Both anchors are a person's act, and both
     are refused unless the value really is at that place;
  3. `anchor check` — both ok;
  4. the deck is revised. The conversion figure moves to a new appendix slide;
     net revenue does not move;
  5. `anchor check` again — one `ok`, one `anchor_lost`.

Step 5 is the point. The number is still IN the file, two slides further on,
and the tool says `anchor_lost` anyway. It does not follow. Where the figure
went is a question this tool refuses to answer, because answering it wrongly is
how a ledger starts citing the wrong slide.

Everything is written under --out (default ./anchor-demo/): its own database,
its own copy of the deck. Nothing of yours is touched.

Usage:  python anchor_demo.py [--out DIR]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FIXTURES = REPO / "orchestrator-backend" / "tests" / "fixtures" / "artifacts"

try:                                                        # the installed package (FL-006) ...
    from provledger import db as odb
    from provledger.artifacts import anchor as an
except ModuleNotFoundError:                                 # ... or the bundled source tree
    BACKEND = REPO / "orchestrator-backend"                 # on its own line: only the host shim
    sys.path.insert(0, str(BACKEND))                        # may name that path and splice it in one breath
    from orchestrator import db as odb
    from orchestrator.artifacts import anchor as an

PROJECT = "phantom-uplift"
DECK = "decks/q3_review.pptx"
ANCHORS = [("slide 4", "metric:q3_conv", "3.2"), ("slide 2", "metric:net_revenue", "64.33")]


def _say(line: str = "") -> None:
    print(line, flush=True)


def _check(conn, out_dir: Path, heading: str) -> tuple[int, int]:
    _say(heading)
    rows = an.check_project(conn, PROJECT, root=str(out_dir))
    for r in rows:
        _say(f"  {r['state']:12} {r['node_key']:20} {r['value']:>7}  {r['where']:<10} {r['file']}")
        if r["reason"]:
            _say(f"               {r['reason']}")
        if r["found_instead"]:
            _say(f"               what is there now: {r['found_instead']}")
    ok = sum(1 for r in rows if r["state"] == "ok")
    return ok, len(rows) - ok


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default=str(HERE / "anchor-demo"), help="where the demo writes (default ./anchor-demo)")
    args = p.parse_args(argv)
    out_dir = Path(args.out).resolve()
    (out_dir / "decks").mkdir(parents=True, exist_ok=True)
    deck = out_dir / DECK
    shutil.copy(FIXTURES / "deck_v1.pptx", deck)

    conn = odb.open_db(out_dir / "anchor-demo.db")
    odb.run_migrations(conn)
    for name, value, unit in (("q3_conv", 3.2, "percent"), ("net_revenue", 64.33, "usd_m")):
        conn.execute("INSERT INTO metrics (project, name, value, unit, source) VALUES (?, ?, ?, ?, 'demo')",
                     (PROJECT, name, value, unit))
    conn.commit()

    _say("1. the deck as it went out")
    for at, node, value in ANCHORS:
        row = an.anchor(conn, PROJECT, deck, at, node, value, stored_path=DECK)
        _say(f"  {row['node_key']} ← {row['value_text']} at {row['where']} in {row['file']['path']}")
        _say(f"    occurrence {row['id']} · tier {row['tier']} · by {row['by']} · sha256 {row['file']['sha256'][:12]}…")
    _say()
    _check(conn, out_dir, "2. anchor check — the deck still says what it said")
    _say()

    _say("3. the deck is revised: the conversion figure moves to an appendix slide")
    shutil.copy(FIXTURES / "deck_v2.pptx", deck)
    _say()
    ok, lost = _check(conn, out_dir, "4. anchor check — one number moved, and the tool does not follow it")
    _say()
    _say(f"   {ok} ok · {lost} anchor_lost")
    _say("   the figure is still in the file, on slide 6. The anchor says lost anyway:")
    _say("   a lost anchor is reported, never re-pointed — citing the wrong slide is worse than")
    _say("   admitting the pointer broke.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
