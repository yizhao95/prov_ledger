"""review_run.py — the deterministic review driver (SKILL.md steps 0–4c), driven
end to end through the public write path only: publish-plan.sh -> start/complete
-step.sh -> review_run.py (which itself calls init_project.sh, reason-slots.sh,
reason-fill.sh, complete-step.sh / fail-step.sh). Fully isolated: ORCH_DB,
PSG_REGISTRY_PATH and PSG_REGISTRY_ROOT point into tmp_path."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from test_review_diff import _git, repo  # noqa: F401  (the git fixture: pipeline.run -> old_name)

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
SKILLS = SCRIPTS.parents[1]
REVIEW_RUN = SCRIPTS / "review_run.py"
PUBLISH = SKILLS / "writing-plans" / "scripts" / "publish-plan.sh"
EXEC = SKILLS / "executing-plans" / "scripts"
INIT = SKILLS / "project-state-graph" / "scripts" / "init_project.sh"
PROJECT = "proj"

BODY_CHANGE = "def run():\n    return old_name() + 0\n\ndef old_name():\n    return 1\n"
STALE_CHANGE = "def run():\n    return 1\n"                                   # old_name gone, app.main still calls it
SIGNATURE_CHANGE = "def run():\n    return old_name(1)\n\ndef old_name(x):\n    return x\n"   # app.main calls old_name() -> break


class WS:
    """One isolated workspace: a git repo, its own registry root and orchestrator DB."""

    def __init__(self, tmp_path: Path, repo: Path):
        self.repo = repo
        self.graphs = tmp_path / "graphs"
        self.graphs.mkdir()
        self.registry = self.graphs / "projects.json"
        self.orch_db = tmp_path / "orch.db"
        self.env = {**os.environ, "ORCH_DB": str(self.orch_db),
                    "PSG_REGISTRY_PATH": str(self.registry), "PSG_REGISTRY_ROOT": str(self.graphs)}
        # a second file whose caller is NOT touched by the diffs below
        (repo / "app.py").write_text("from pipeline import old_name\n\ndef main():\n    return old_name()\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "app")
        self.sh(["bash", str(INIT), "--name", PROJECT, "--repo", str(repo)])       # baseline graph, run 1
        self.sha0 = self.registered_sha()

    def sh(self, cmd, cwd=None, check=True) -> subprocess.CompletedProcess:
        p = subprocess.run(cmd, cwd=cwd or self.repo, env=self.env, capture_output=True, text=True, timeout=120)
        if check:
            assert p.returncode == 0, f"{cmd}\n{p.stdout}\n{p.stderr}"
        return p

    def registered_sha(self) -> str:
        return next(p for p in json.loads(self.registry.read_text())["projects"] if p["name"] == PROJECT)["commit_sha"]

    def op(self, name: str, payload: dict, check=True) -> subprocess.CompletedProcess:
        f = self.graphs / f"{name}-{payload.get('step_id') or payload.get('plan_id')}.json"
        f.write_text(json.dumps(payload))
        return self.sh(["bash", str(EXEC / f"{name}.sh"), str(f)], check=check)

    def change(self, pipeline_src: str) -> str:
        (self.repo / "pipeline.py").write_text(pipeline_src)
        _git(self.repo, "commit", "-aqm", "change")
        return _git(self.repo, "rev-parse", "HEAD")

    def needs_review_plan(self, goal="tweak run") -> str:
        """Publish a one-step plan declared on the project and drive it to NEEDS_REVIEW."""
        inp = self.graphs / "plan-input.json"
        inp.write_text(json.dumps({"goal": goal, "prefix": "rv", "project": PROJECT,
                                   "declared_targets": ["pipeline.run"],
                                   "steps": [{"description": "CODE: the change", "type": "CODE"}]}))
        out = json.loads(self.sh(["bash", str(PUBLISH), str(inp)]).stdout)
        plan_id, step_id = out["plan_id"], out["step_ids"][0]
        self.op("start-step", {"step_id": step_id})
        tail = json.loads(self.op("complete-step", {"step_id": step_id, "summary": "done"}).stdout.strip().splitlines()[-1])
        assert tail.get("needs_agent_review") is True, tail
        return plan_id

    def review_run(self, plan_id: str, *args: str) -> subprocess.CompletedProcess:
        return self.sh([sys.executable, str(REVIEW_RUN), "--plan-id", plan_id, "--project", PROJECT,
                        "--json", *args], check=False)

    # ── reads (test assertions only) ─────────────────────────────────────────
    def q(self, sql, *params):
        c = sqlite3.connect(str(self.orch_db))
        try:
            return c.execute(sql, params).fetchall()
        finally:
            c.close()

    def plan(self, plan_id):
        return self.q("SELECT status, review_state FROM Plans WHERE plan_id=?", plan_id)[0]

    def step(self, step_id):
        return self.q("SELECT status, COALESCE(log_context,'') FROM Steps WHERE step_id=?", step_id)[0]

    def reasons(self, plan_id):
        return self.q("SELECT node_key, text FROM node_reason WHERE plan_id=? AND kind='reason'", plan_id)

    def runs(self) -> int:
        db = next(p for p in json.loads(self.registry.read_text())["projects"] if p["name"] == PROJECT)["db_path"]
        c = sqlite3.connect(db)
        try:
            return c.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0]
        finally:
            c.close()


def _json_tail(p: subprocess.CompletedProcess) -> dict:
    lines = [ln for ln in p.stdout.strip().splitlines() if ln.startswith("{")]
    assert lines, p.stdout + p.stderr
    return json.loads(lines[-1])


@pytest.fixture
def ws(tmp_path, repo):
    return WS(tmp_path, repo)


def test_happy_path_locks_sha_before_refresh_and_fills_stub_reasons(ws):
    head = ws.change(BODY_CHANGE)
    plan_id = ws.needs_review_plan()
    p = ws.review_run(plan_id, "--reasons", "stub")
    assert p.returncode == 0, p.stdout + p.stderr
    out = _json_tail(p)
    assert out["closed"] == "COMPLETED" and out["verdict"] is True
    assert out["slots"] == 1 and out["filled"] == 1 and out["unstated"] == 0
    assert out["refreshed_sha"] == head
    assert ws.plan(plan_id) == ("COMPLETED", "reviewed")
    # step 0: the lock line names the PRE-refresh sha and sits in the review step's log
    review_status, review_log = ws.step(f"{plan_id}-REVIEW")
    assert f"[REVIEW LOCK] registered_sha={ws.sha0}" in review_log
    assert ws.registered_sha() == head != ws.sha0          # the refresh moved the registry afterwards
    child_status, child_log = ws.step(f"{plan_id}-REVIEW.1")
    assert child_status == "COMPLETED"
    assert "tests skipped" in child_log.lower()            # no --tests given -> logged, not silent
    (nk, text), = ws.reasons(plan_id)
    assert text.startswith("scenario:") and "node_changed" in text


def test_tests_command_failure_fails_the_review(ws):
    ws.change(BODY_CHANGE)
    plan_id = ws.needs_review_plan()
    p = ws.review_run(plan_id, "--reasons", "stub", "--tests", "echo boom; false")
    assert p.returncode == 1
    assert _json_tail(p)["closed"] == "FAILED"
    assert ws.plan(plan_id)[0] == "FAILED"
    status, log = ws.step(f"{plan_id}-REVIEW.1")
    assert status == "FAILED" and "boom" in log


def test_reasons_unstated_writes_null_for_every_slot(ws):
    ws.change(BODY_CHANGE)
    plan_id = ws.needs_review_plan()
    p = ws.review_run(plan_id, "--reasons", "unstated")
    assert p.returncode == 0, p.stdout + p.stderr
    out = _json_tail(p)
    assert (out["slots"], out["filled"], out["unstated"]) == (1, 0, 1)
    assert [t for _, t in ws.reasons(plan_id)] == [None]
    assert ws.plan(plan_id) == ("COMPLETED", "reviewed")


def test_reasons_file_unknown_key_exits_6_then_resumes(ws, tmp_path):
    ws.change(BODY_CHANGE)
    plan_id = ws.needs_review_plan()
    p = ws.review_run(plan_id, "--reasons", "ask")               # look at the checklist first
    assert p.returncode == 6, p.stdout + p.stderr
    assert "pipeline.run" in (p.stdout + p.stderr)
    assert ws.step(f"{plan_id}-REVIEW.1")[0] == "IN_PROGRESS"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"qualified_name": "pipeline.nope", "text": "x"}]))
    p = ws.review_run(plan_id, "--reasons", str(bad))
    assert p.returncode == 6, p.stdout + p.stderr
    assert "pipeline.nope" in (p.stdout + p.stderr)
    assert ws.step(f"{plan_id}-REVIEW.1")[0] == "IN_PROGRESS"     # nothing closed, nothing written
    assert ws.reasons(plan_id) == []
    good = tmp_path / "good.json"
    good.write_text(json.dumps([{"qualified_name": "pipeline.run", "text": "loosened for the test"}]))
    runs_before = ws.runs()
    p = ws.review_run(plan_id, "--reasons", str(good))          # resumes at 4c: no second refresh
    assert p.returncode == 0, p.stdout + p.stderr
    assert ws.runs() == runs_before
    assert ws.plan(plan_id) == ("COMPLETED", "reviewed")
    assert [t for _, t in ws.reasons(plan_id)] == ["loosened for the test"]


def test_4a_stale_reference_fails_the_plan_without_refreshing(ws):
    ws.change(STALE_CHANGE)
    plan_id = ws.needs_review_plan("drop old_name")
    p = ws.review_run(plan_id, "--reasons", "stub")
    assert p.returncode == 1
    assert _json_tail(p)["gates"]["stale_references"] is False
    assert ws.plan(plan_id)[0] == "FAILED"
    status, log = ws.step(f"{plan_id}-REVIEW.1")
    assert status == "FAILED" and "stale_references" in log and "app.py" in log and "old_name" in log
    assert ws.registered_sha() == ws.sha0                     # 4a never refreshes


def test_signature_break_fails_without_override(ws):
    ws.change(SIGNATURE_CHANGE)
    plan_id = ws.needs_review_plan("old_name takes x")
    p = ws.review_run(plan_id, "--reasons", "stub")
    assert p.returncode == 1
    gates = _json_tail(p)["gates"]
    assert gates["signature"] is False and gates["stale_references"] is True
    assert ws.plan(plan_id)[0] == "FAILED"


def test_accept_signature_overrides_only_that_gate_and_leaves_a_trace(ws):
    ws.change(SIGNATURE_CHANGE)
    plan_id = ws.needs_review_plan("old_name takes x")
    p = ws.review_run(plan_id, "--reasons", "stub", "--accept-signature", "app.main is updated in the next plan")
    assert p.returncode == 0, p.stdout + p.stderr
    assert ws.plan(plan_id) == ("COMPLETED", "reviewed")
    status, log = ws.step(f"{plan_id}-REVIEW.1")
    assert status == "COMPLETED"
    assert "signature gate overridden" in log and "app.main is updated in the next plan" in log


def test_accept_signature_cannot_override_a_stale_reference(ws):
    ws.change(STALE_CHANGE)
    plan_id = ws.needs_review_plan("drop old_name")
    p = ws.review_run(plan_id, "--reasons", "stub", "--accept-signature", "not applicable")
    assert p.returncode == 1
    assert ws.plan(plan_id)[0] == "FAILED"


def test_dry_run_writes_nothing(ws):
    ws.change(BODY_CHANGE)
    plan_id = ws.needs_review_plan()
    runs_before = ws.runs()
    p = ws.review_run(plan_id, "--reasons", "stub", "--dry-run")
    assert p.returncode == 0, p.stdout + p.stderr
    out = _json_tail(p)
    assert out["verdict"] is True and out["closed"] is None
    assert ws.step(f"{plan_id}-REVIEW")[0] == "NEEDS_REVIEW"
    assert ws.step(f"{plan_id}-REVIEW.1")[0] == "PENDING"
    assert "[REVIEW LOCK]" not in ws.step(f"{plan_id}-REVIEW")[1]
    assert ws.registered_sha() == ws.sha0 and ws.runs() == runs_before
    assert ws.reasons(plan_id) == []
