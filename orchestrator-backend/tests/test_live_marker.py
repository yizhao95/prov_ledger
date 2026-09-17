"""A test that reads the live ledger is opt-in, and the thresholds are checked on fixtures.

H1 and H4 are real acceptance items, but three of their tests read
`~/skill-workspace/orchestrator.db` — so their verdict depends on who happened
to be running plans on this machine an hour ago. That makes them useful for a
person and useless in a suite: the same commit passes or fails depending on the
ledger, and a failure says nothing about the diff.

So the live ones are marked `live`, deselected by default, and say out loud how
to run them; the threshold LOGIC they assert is checked on fixtures, where the
numbers are built on purpose and the assertion means something about the code.
"""
from __future__ import annotations

import ast
import os
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "orchestrator-backend"
LIVE_TESTS = {
    "tests/test_plan_metrics.py": ("test_h1_threshold",),
    "tests/test_overhead.py": ("test_h1_overhead_ratio", "test_h4_context_overhead"),
}


def _decorators(path: Path, name: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return [ast.unparse(d) for d in fn.decorator_list]


def test_the_live_marker_is_registered_and_deselected_by_default():
    cfg = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("live:") for m in cfg["markers"]), "the live marker must be registered, not improvised"
    assert "not live" in cfg["addopts"], "a live-ledger test must not run unless it is asked for"


@pytest.mark.parametrize("rel,names", sorted(LIVE_TESTS.items()))
def test_every_live_ledger_test_carries_the_marker(rel, names):
    path = BACKEND / rel
    for name in names:
        assert "pytest.mark.live" in _decorators(path, name), f"{rel}::{name} reads the live ledger without the marker"


@pytest.mark.parametrize("rel,names", sorted(LIVE_TESTS.items()))
def test_a_live_test_skips_with_the_reason_when_it_was_not_asked_for(rel, names, monkeypatch):
    monkeypatch.delenv("PROVLEDGER_LIVE", raising=False)
    module = __import__(Path(rel).stem)
    for name in names:
        fn = getattr(module, name)
        with pytest.raises(pytest.skip.Exception, match="PROVLEDGER_LIVE"):
            fn(**{k: None for k in fn.__code__.co_varnames[:fn.__code__.co_argcount]})


def test_the_h1_threshold_logic_is_checked_on_a_fixture(conn, tmp_path, monkeypatch):
    """Three plans, one of them over the line: the comparison must name exactly
    that one. This is the assertion CI can make about the code — the live test
    can only report what today's ledger happens to hold."""
    from orchestrator import plan_metrics
    reg = tmp_path / "projects.json"
    reg.write_text('{"projects": [{"name": "proj", "repo": "/home/x/repo", "db_path": "/tmp/g.db", "commit_sha": "c"}]}')
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    calls = {"cheap": 2, "normal": 6, "expensive": 40}      # over 2 steps: 1.0 / 3.0 / 20.0
    for i, (name, n) in enumerate(calls.items()):
        conn.execute("INSERT INTO Plans (plan_id, original_goal, status, created_at, completed_at, project, project_source) "
                     "VALUES (?, 'g', 'COMPLETED', ?, ?, 'proj', 'declared')",
                     (name, f"2026-09-15 1{i}:00:00", f"2026-09-15 1{i}:30:00"))
        for s in range(2):
            conn.execute("INSERT INTO Steps (step_id, plan_id, description, status, execution_order, depth_level, "
                         "log_context, step_type) VALUES (?, ?, 'd', 'COMPLETED', ?, 0, '', 'COMMAND')",
                         (f"{name}-{s}", name, s))
        for k in range(n):
            conn.execute("INSERT INTO tool_call_log (session_id, cwd, tool_name, at) VALUES ('s', '/home/x/repo', 'Bash', ?)",
                         (f"2026-09-15 1{i}:{k % 30:02d}:0{k % 10}.000",))
    conn.commit()
    baseline = {"calls_per_step": {"median": 2.0, "p90": 4.0, "n": 3}}
    over = plan_metrics.h1_exceedances(conn, baseline, last=3)
    assert [p for p, _ in over] == ["expensive"], over
    assert over[0][1] == 20.0
    assert plan_metrics.h1_exceedances(conn, {"calls_per_step": {"p90": 100.0}}, last=3) == []
    assert plan_metrics.h1_exceedances(conn, {"calls_per_step": {"p90": None}}, last=3) is None


def test_the_budget_logic_is_checked_on_a_fixture():
    """H1's two ratios and H4's context budget are the spec's written numbers
    (§20). Checking them here means a change to the budget is a change to a
    test, not a change to whatever the live ledger says today."""
    from orchestrator import plan_metrics
    assert plan_metrics.BUDGETS == {"provenance_ratio": 0.10, "overhead_ratio": 0.35, "context_overhead_tokens": 3000}
    inside = {"provenance_ratio": 0.05, "overhead_ratio": 0.30, "context_overhead_tokens": 2999}
    assert plan_metrics.over_budget(inside) == []
    outside = {"provenance_ratio": 0.11, "overhead_ratio": 0.30, "context_overhead_tokens": 3001}
    assert plan_metrics.over_budget(outside) == ["provenance_ratio", "context_overhead_tokens"]
    assert plan_metrics.over_budget({"provenance_ratio": None}) == []      # nothing measured is not a breach
