"""Timeline scenarios: one test per tests/scenarios/cases/<name>/scenario.toml.

Each case runs its tasks through the public write path in an isolated
workspace, checks expect.events / must_not, and compares the normalised event
stream with golden.json byte for byte. PROVLEDGER_UPDATE_GOLDEN=1 rewrites the
golden and skips the comparison (review the diff like code)."""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from tests.scenarios import runner

HERE = Path(__file__).resolve().parent
CASES = HERE / "scenarios" / "cases"
FIXTURES = HERE / "scenarios" / "fixtures"


def iter_scenarios() -> list[Path]:
    return sorted(p.parent for p in CASES.glob("*/scenario.toml"))


def load_scenario(case: Path) -> dict:
    return tomllib.loads((case / "scenario.toml").read_text(encoding="utf-8"))


def golden_text(norm: dict) -> str:
    return json.dumps(norm, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


@pytest.mark.parametrize("case", iter_scenarios(), ids=lambda p: p.name)
def test_scenario(case: Path, tmp_path: Path):
    sc = load_scenario(case)
    assert "must_not" in sc.get("expect", {}), f"{case.name}: every scenario must declare must_not"
    ws = runner.make_workspace(tmp_path, FIXTURES / sc["scenario"]["fixture"])
    result = runner.run_scenario(ws, sc)
    errors = runner.check_expect(result["norm"], sc["expect"])
    assert errors == [], f"{case.name}:\n  " + "\n  ".join(errors)
    text = golden_text(result["norm"])
    golden = case / "golden.json"
    if os.environ.get("PROVLEDGER_UPDATE_GOLDEN") == "1":
        golden.write_text(text, encoding="utf-8")
        pytest.skip("golden updated")
    assert golden.exists(), f"{case.name}: no golden.json — run once with PROVLEDGER_UPDATE_GOLDEN=1"
    assert golden.read_text(encoding="utf-8") == text, f"{case.name}: normalised events differ from golden.json"
