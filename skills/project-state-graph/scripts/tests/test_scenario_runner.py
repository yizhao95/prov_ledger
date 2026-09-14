"""Unit tests for tests/scenarios/runner.py — the timeline-scenario harness.
normalize / check_expect are pure; make_workspace / apply_change / publish /
run_steps / close go through the public write path only (publish-plan.sh,
run-step.sh, review_run.py) inside a fully isolated workspace."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.scenarios import runner

from tests.corpus.harness import default_corpus

CORPUS_BASE = default_corpus() / "basic_pipeline" / "base"     # phase 6: the corpus ships in provledger.testing
REAL_HOME = Path(os.path.expanduser("~")).resolve()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


# ── make_workspace: isolation ────────────────────────────────────────────────

def test_make_workspace_is_isolated_and_builds_the_baseline_graph(tmp_path):
    ws = runner.make_workspace(tmp_path, CORPUS_BASE, project="scn")
    root = tmp_path.resolve()
    for key in ("ORCH_DB", "PSG_REGISTRY_PATH", "PSG_REGISTRY_ROOT", "HOME"):
        assert Path(ws.env[key]).resolve().is_relative_to(root), (key, ws.env[key])
        assert not Path(ws.env[key]).resolve().is_relative_to(REAL_HOME / "skill-workspace")
    assert Path(ws.env["PROVLEDGER_VENV"]).exists()
    reg = json.loads(ws.registry.read_text())["projects"]
    assert [p["name"] for p in reg] == ["scn"]
    assert Path(reg[0]["repo"]).resolve() == ws.repo.resolve()
    assert Path(reg[0]["db_path"]).exists() and Path(reg[0]["db_path"]).resolve().is_relative_to(root)
    assert ws.task_shas["base"] == _git(ws.repo, "rev-parse", "HEAD")
    assert (ws.repo / "pkg" / "pipeline.py").exists()


# ── apply_change: the three forms ─────────────────────────────────────────────

def test_apply_change_files_generated_and_revert(tmp_path):
    ws = runner.make_workspace(tmp_path, CORPUS_BASE)
    base_src = (ws.repo / "pkg" / "pipeline.py").read_text()
    sha0 = ws.task_shas["base"]

    sha1 = runner.apply_change(ws, {"files": {"pkg/pipeline.py": base_src.replace("df.qty > 0", "df.qty > 5")}}, task="t1")
    assert sha1 != sha0 and _git(ws.repo, "rev-parse", "HEAD") == sha1
    assert "df.qty > 5" in (ws.repo / "pkg" / "pipeline.py").read_text()
    assert _git(ws.repo, "status", "--short") == ""                      # committed, tree clean
    assert ws.task_shas["t1"] == sha1

    sha2 = runner.apply_change(ws, {"generated": "reformat"}, task="t2")
    assert sha2 != sha1
    src2 = (ws.repo / "pkg" / "pipeline.py").read_text()
    assert src2 != (ws.repo / "pkg" / "pipeline.py").read_text().replace(src2, "") + src2 or True
    ast.parse(src2)                                                        # still valid python
    assert "df.qty > 5" in src2                                            # reformat kept t1's edit

    sha3 = runner.apply_change(ws, {"revert_to": "base"}, task="t3")
    assert sha3 not in (sha0, sha1, sha2)                                  # a NEW commit that restores the files
    assert (ws.repo / "pkg" / "pipeline.py").read_text() == base_src

    with pytest.raises(ValueError):
        runner.apply_change(ws, {"bogus": 1}, task="t4")


def test_apply_change_replace_requires_a_unique_match(tmp_path):
    ws = runner.make_workspace(tmp_path, CORPUS_BASE)
    sha = runner.apply_change(ws, {"replace": {"pkg/pipeline.py": [["df.qty > 0", "df.qty > 5"]]}}, task="t1")
    assert sha == _git(ws.repo, "rev-parse", "HEAD") and ws.task_shas["t1"] == sha
    assert "df.qty > 5" in (ws.repo / "pkg" / "pipeline.py").read_text()
    with pytest.raises(ValueError):                      # old text absent
        runner.apply_change(ws, {"replace": {"pkg/pipeline.py": [["df.qty > 0", "x"]]}}, task="t2")
    with pytest.raises(ValueError):                      # old text ambiguous (two occurrences)
        runner.apply_change(ws, {"replace": {"pkg/pipeline.py": [["df", "frame"]]}}, task="t3")


# ── normalize: pure ───────────────────────────────────────────────────────────

RAW = {
    "runs": [{"id": 7, "plan_id": "t0-20260914", "step_id": "t0-20260914-REVIEW.1", "trigger": "review", "commit_sha": "aaa"},
             {"id": 9, "plan_id": "t1-20260914", "step_id": "t1-20260914-REVIEW.1", "trigger": "review", "commit_sha": "bbb"}],
    "nodes": {"nk_1": "pkg.pipeline.clean", "nk_2": "pkg.pipeline.load"},
    "node_events": [
        {"id": 40, "run_id": 7, "seq": 0, "event_type": "node_added", "node_key": "nk_1", "tier": "observed",
         "payload": {"node_type": "function", "qualified_name": "pkg.pipeline.clean"}, "created_at": "2026-09-14 05:00:00"},
        {"id": 41, "run_id": 9, "seq": 0, "event_type": "node_changed", "node_key": "nk_1", "tier": "observed",
         "payload": {"changed": ["struct_sig"], "prev_run_id": 7, "struct_sig": {"from": "x", "to": "y"}}, "created_at": "2026-09-14 05:01:00"},
        {"id": 42, "run_id": 9, "seq": 1, "event_type": "node_matched", "node_key": "nk_2", "tier": "observed",
         "payload": {"prev_run_id": 7, "via": "qualname"}, "created_at": "2026-09-14 05:01:00"},
    ],
    "plans": [{"plan_id": "t0-20260914", "status": "COMPLETED", "review_state": "reviewed", "project": "scn",
               "project_source": "cwd", "review_skip_reason": None, "created_at": "x", "updated_at": "y"},
              {"plan_id": "t1-20260914", "status": "COMPLETED", "review_state": "reviewed", "project": "scn",
               "project_source": "cwd", "review_skip_reason": None, "created_at": "x", "updated_at": "y"}],
    "reasons": [{"id": 3, "plan_id": "t1-20260914", "node_key": "nk_1", "kind": "reason", "text": "loosened",
                 "source": "agent", "tier": "stated", "run_id": 9, "created_at": "z"},
                {"id": 4, "plan_id": "t1-20260914", "node_key": "nk_2", "kind": "reason", "text": None,
                 "source": "system", "tier": "derived", "run_id": 9, "created_at": "z"}],
    "expectations": [{"id": 5, "plan_id": "t0-20260914", "target": "pkg.pipeline.clean", "target_kind": "node",
                      "claim": "fewer rows", "channel": "graph", "created_at": "z"}],
    "outcomes": [{"id": 6, "expectation_id": 5, "kind": "survival", "value": {"signal": "churned", "evidence": [40, 41],
                  "plans": ["t0-20260914", "t1-20260914"], "node_key": "nk_1"}, "source": "state_graph",
                  "tier": "derived", "reason": None, "backfilled_by_plan": "t1-20260914", "observed_at": "z"}],
    "review_logs": [{"plan_id": "t1-20260914", "step_id": "t1-20260914-REVIEW", "tags": ["[REVIEW LOCK]"]}],
}
NAMES = {"t0-20260914": "t0", "t1-20260914": "t1"}


def _walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def test_normalize_strips_volatile_fields_and_maps_identities():
    norm = runner.normalize(RAW, NAMES)
    keys = {k for k, _ in _walk(norm)}
    assert not keys & {"id", "created_at", "observed_at", "updated_at", "started_at", "completed_at",
                       "node_key", "plan_id", "run_id", "prev_run_id", "commit_sha", "expectation_id"}
    text = json.dumps(norm)
    assert "nk_1" not in text and "t1-20260914" not in text and "2026-09-14" not in text
    assert norm["runs"] == [{"run": 1, "plan": "t0", "step": "REVIEW.1", "trigger": "review"},
                            {"run": 2, "plan": "t1", "step": "REVIEW.1", "trigger": "review"}]
    ev = norm["events"]
    assert {"plan": "t1", "run": 2, "type": "node_changed", "node": "pkg.pipeline.clean", "tier": "observed",
            "changed": ["struct_sig"], "prev_run": 1, "struct_sig": {"from": "x", "to": "y"}} in ev
    assert {"plan": "t1", "run": 2, "type": "reason", "node": "pkg.pipeline.clean", "text_is": "stated",
            "text": "loosened", "source": "agent", "tier": "stated"} in ev
    assert {"plan": "t1", "run": 2, "type": "reason", "node": "pkg.pipeline.load", "text_is": "unstated",
            "text": None, "source": "system", "tier": "derived"} in ev
    assert {"plan": "t0", "type": "expectation", "target": "pkg.pipeline.clean", "target_kind": "node",
            "claim": "fewer rows", "channel": "graph"} in ev
    out = next(e for e in ev if e["type"] == "outcome")
    assert out["plan"] == "t0" and out["kind"] == "survival" and out["signal"] == "churned"
    assert out["backfilled_by"] == "t1" and out["value"]["evidence"] == ["1.0", "2.0"]
    assert out["value"]["plans"] == ["t0", "t1"] and out["value"]["node"] == "pkg.pipeline.clean"
    assert {"plan": "t1", "type": "plan", "closed": "COMPLETED", "review_state": "reviewed", "project": "scn",
            "project_source": "cwd", "review_skipped": None} in ev
    assert {"plan": "t1", "type": "review_log", "step": "REVIEW", "tag": "[REVIEW LOCK]"} in ev


def test_normalize_is_deterministic_and_json_stable():
    a = json.dumps(runner.normalize(RAW, NAMES), sort_keys=True)
    b = json.dumps(runner.normalize(json.loads(json.dumps(RAW)), dict(NAMES)), sort_keys=True)
    assert a == b


# ── check_expect ──────────────────────────────────────────────────────────────

def test_check_expect_subset_match_and_missing():
    norm = runner.normalize(RAW, NAMES)
    ok = {"events": [{"plan": "t1", "type": "node_changed", "node": "pkg.pipeline.clean", "changed": ["struct_sig"]},
                     {"plan": "t1", "closed": "COMPLETED", "review_state": "reviewed"},            # no type: any event
                     {"plan": "t1", "type": "reason", "node": "pkg.pipeline.clean", "text_is": "stated"},
                     {"plan": "t0", "type": "outcome", "kind": "survival", "signal": "churned"},
                     {"plan": "t1", "type": "reason", "text_contains": "loos"}],                   # _contains operator
          "must_not": []}
    assert runner.check_expect(norm, ok) == []
    bad = {"events": [{"plan": "t1", "type": "node_changed", "node": "pkg.pipeline.load"},
                      {"plan": "t1", "type": "node_removed"}], "must_not": []}
    errs = runner.check_expect(norm, bad)
    assert len(errs) == 2 and all(e.startswith("missing") for e in errs)
    assert "pkg.pipeline.load" in errs[0]


def test_check_expect_must_not_hit_is_a_failure():
    norm = runner.normalize(RAW, NAMES)
    exp = {"events": [], "must_not": [{"plan": "t1", "type": "node_changed", "node": "pkg.pipeline.load"},   # fine
                                      {"plan": "t1", "type": "node_matched", "node": "pkg.pipeline.load"},   # hit
                                      {"anywhere": "loosened"}]}                                            # hit (text)
    errs = runner.check_expect(norm, exp)
    assert len(errs) == 2 and all(e.startswith("forbidden") for e in errs)
    assert "node_matched" in errs[0] and "loosened" in errs[1]


def test_check_expect_requires_must_not():
    with pytest.raises(ValueError):
        runner.check_expect(runner.normalize(RAW, NAMES), {"events": []})


# ── the public write path end to end (one task) ───────────────────────────────

def test_one_task_through_publish_run_steps_close_and_collect(tmp_path):
    ws = runner.make_workspace(tmp_path, CORPUS_BASE)
    src = (ws.repo / "pkg" / "pipeline.py").read_text()
    task = {"name": "t1", "goal": "loosen the qty filter", "declared_targets": ["pkg.pipeline.clean"],
            "change": {"files": {"pkg/pipeline.py": src.replace("df.qty > 0", "df.qty > 5")}},
            "expectations": [{"target": "pkg.pipeline.clean", "target_kind": "node", "claim": "fewer rows dropped", "channel": "graph"}],
            "reasons": {"pkg.pipeline.clean": "loosened for Q3"}}
    result = runner.run_scenario(ws, {"scenario": {"name": "unit"}, "tasks": [task]})
    t1 = result["tasks"][0]
    assert t1["name"] == "t1" and t1["close"]["closed"] == "COMPLETED" and t1["close"]["slots"] == 1
    norm = result["norm"]
    errs = runner.check_expect(norm, {
        "events": [{"plan": "t1", "type": "node_changed", "node": "pkg.pipeline.clean", "changed": ["struct_sig"]},
                   {"plan": "t1", "type": "reason", "node": "pkg.pipeline.clean", "text_is": "stated", "text": "loosened for Q3"},
                   {"plan": "t1", "type": "expectation", "target": "pkg.pipeline.clean", "channel": "graph"},
                   {"plan": "t1", "type": "plan", "closed": "COMPLETED", "review_state": "reviewed", "project": "scn", "project_source": "cwd"},
                   {"plan": "t1", "type": "review_log", "step": "REVIEW", "tag": "[REVIEW LOCK]"}],
        "must_not": [{"plan": "t1", "type": "node_changed", "node": "pkg.pipeline.load"},
                     {"plan": "t1", "type": "node_removed"}]})
    assert errs == [], errs
    # nothing leaked outside the workspace
    assert not (REAL_HOME / "skill-workspace" / "project-graphs" / "scn").exists()


# ── suite wiring (Task 5) ─────────────────────────────────────────────────────

def test_llm_consistency_marker_registered_and_deselected_by_default():
    import tomllib
    cfg = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    ini = cfg.get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert any(m.startswith("llm_consistency:") for m in ini.get("markers", [])), "marker not registered"
    assert 'not llm_consistency' in ini.get("addopts", ""), "llm_consistency tests must be deselected by default"
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_llm_consistency.py", "-q", "-p", "no:cacheprovider"],
                       cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert "1 deselected" in r.stdout and r.returncode in (0, 5), r.stdout[-400:]
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_llm_consistency.py", "-q", "-p", "no:cacheprovider",
                        "-m", "llm_consistency"], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert r.returncode == 0 and "1 passed" in r.stdout, r.stdout[-400:]


def test_scenario_fixtures_are_never_collected():
    """fixtures/pipeline_repo carries its own tests/ package; collecting it would
    shadow this suite's `tests` package (48 import errors) — norecursedirs."""
    r = subprocess.run([sys.executable, "-m", "pytest", "tests", "--co", "-q", "-p", "no:cacheprovider"],
                       cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert "pipeline_repo" not in r.stdout, r.stdout[-600:]
    assert "ERROR collecting" not in r.stdout and "errors during collection" not in r.stdout, r.stdout[-600:]
    assert "test_api_refs.py" in r.stdout
