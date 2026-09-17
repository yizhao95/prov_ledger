"""The demo's phase-4 act: a deck, an anchor, a revision, a lost anchor
(DP phase 4, Task 3).

`examples/phantom-uplift/anchor_demo.py` is the arc in one file. It anchors two
numbers in the fixture deck to two metrics the demo recorded, checks them (both
ok), drops the revised deck on top of the same path, and checks again: the
conversion figure moved to another slide, so it is `anchor_lost`, and net
revenue never moved, so it is still `ok`. One of each, in one run, which is
exactly what the acceptance item asks to see.

The second test is the one that matters for the machine this runs on: the demo
writes to its own database and to a deck under its own directory, and to
nothing else. Phase 3 shipped a version of the anchoring feature whose suite
wrote ten empty git notes into the developer's own repository; the first rule
of a measuring tool is not to change what it measures.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "phantom-uplift"
SCRIPT = EXAMPLE / "anchor_demo.py"


@pytest.fixture
def workspace(tmp_path):
    """The demo run somewhere it cannot touch anything of the developer's."""
    return tmp_path / "demo"


def _run(workspace: Path):
    r = subprocess.run([sys.executable, str(SCRIPT), "--out", str(workspace)],
                       capture_output=True, text=True, cwd=str(EXAMPLE))
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_the_demo_reports_one_ok_and_one_lost_anchor(workspace):
    out = _run(workspace)
    assert "metric:q3_conv" in out and "metric:net_revenue" in out
    assert out.count("anchor_lost") >= 1 and "ok " in out
    assert "moved or removed" in out
    # the closing line states the arc in numbers so a reader does not have to count
    assert "1 ok · 1 anchor_lost" in out


def test_the_demo_writes_only_its_own_database_and_deck(workspace, tmp_path):
    _run(workspace)
    written = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*") if p.is_file())
    assert written == ["anchor-demo.db", "decks/q3_review.pptx"]
    c = sqlite3.connect(workspace / "anchor-demo.db")
    try:
        assert c.execute("SELECT COUNT(*) FROM occurrence").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM anchor_state").fetchone()[0] == 4    # two checks of two anchors
        # the veto in the demo too: the locator the person typed is still the one on the row
        assert {r[0] for r in c.execute("SELECT locator_json FROM occurrence")} == {
            '{"at": "slide 2", "kind": "pptx", "shape": 2, "slide": 2}',
            '{"at": "slide 4", "kind": "pptx", "shape": 2, "slide": 4}'}
    finally:
        c.close()
