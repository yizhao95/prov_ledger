"""The one door to the live ledger in this suite.

A test that reads ~/skill-workspace/orchestrator.db is judging whatever plans
happened to run on this machine, not the diff in front of it. Those tests are
marked `live` and deselected by default; this helper is what they call first,
so the reason they did not run is printed rather than implied.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

LIVE_DB = Path.home() / "skill-workspace" / "orchestrator.db"
ASK = "set PROVLEDGER_LIVE=1 to check the live ledger"


def require_live_ledger(what: str) -> Path:
    """The live DB path, or a skip that says how to ask for it."""
    if os.environ.get("PROVLEDGER_LIVE") != "1":
        print(f"SKIP-NOTE: {what} — reads the live ledger, so its verdict depends on who ran plans "
              f"on this machine; {ASK}")
        pytest.skip(f"{what}: {ASK} (PROVLEDGER_LIVE)")
    if not LIVE_DB.exists():
        print(f"SKIP-NOTE: {what} — PROVLEDGER_LIVE=1 but there is no ledger at {LIVE_DB}")
        pytest.skip(f"{what}: no orchestrator.db at {LIVE_DB} (PROVLEDGER_LIVE was asked for)")
    return LIVE_DB
