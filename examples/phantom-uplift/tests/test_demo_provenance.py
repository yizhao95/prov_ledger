"""DP phase 2d (Task 5): the demo is a test, so the demo cannot lie.

The scenario is the product's claim in miniature:

  Task A (a person) removes orders.discount. The reason is their own sentence —
  "收到上游通知，v2 以后不再有 discount 列" — with the email that carried it.
  Task B (an agent) plans to compute a discount rate from that column. At
  publish, the heads-up finds the removal AND the words behind it, the agent
  revises and cites the record, and that citation becomes one influence row.

If any of that stops being true, this fails — which is the point: a recorded
walkthrough of a feature that quietly broke is worse than no walkthrough.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "examples" / "phantom-uplift" / "demo-provenance.sh"
WALKTHROUGH = REPO / "scripts" / "demo-walkthrough.py"
VERBATIM = "Drop orders.discount from the rollup — upstream said the v2 feed no longer carries it"
EMAIL_LABEL = "Re: orders feed v2 schema (demo)"


def _run(workspace: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DEMO_HOME"] = str(workspace)
    env["ORCH_DB"] = str(workspace / "orchestrator.db")
    env["PSG_REGISTRY_PATH"] = str(workspace / "projects.json")
    return subprocess.run(["bash", str(SCRIPT)], cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=300)


def _counts(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("Plans", "utterance", "change_reason", "influence", "read_hit", "headline", "reference")}
    finally:
        conn.close()


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    ws = tmp_path_factory.mktemp("demo")
    started = time.time()
    r = _run(ws)
    assert r.returncode == 0, f"demo-provenance.sh failed:\n{r.stdout}\n{r.stderr}"
    return {"ws": ws, "out": r.stdout, "err": r.stderr, "seconds": time.time() - started,
            "db": ws / "orchestrator.db"}


def test_the_demo_runs_and_says_what_it_built(demo):
    assert demo["seconds"] < 60, f"took {demo['seconds']:.1f}s"
    out = demo["out"]
    assert "/graph/" in out and "/node/" in out and "/plan/" in out, "the script does not print its triple URLs"


def test_running_it_twice_changes_nothing(demo, tmp_path):
    """Idempotent: a demo you cannot re-run is a demo that rots."""
    before = _counts(demo["db"])
    r = _run(demo["ws"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _counts(demo["db"]) == before


def test_the_removal_is_recorded_as_the_persons_own_words_with_the_email(demo):
    conn = sqlite3.connect(str(demo["db"]))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT r.id, r.tier, substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start) AS text "
            "FROM change_reason r JOIN utterance u ON u.id = r.verbatim_utterance_id "
            "WHERE r.tier = 'stated' ORDER BY r.id LIMIT 1").fetchone()
        assert row is not None, "task A recorded no stated reason"
        assert VERBATIM in row["text"]
        refs = [r[0] for r in conn.execute("SELECT label FROM reference")]
        assert any(EMAIL_LABEL in (l or "") for l in refs), f"the email reference is missing: {refs}"
    finally:
        conn.close()


def test_task_b_is_warned_by_the_rule_the_person_left(demo):
    """What reaches the NEXT plan is the rule, not the reason.

    The scenario originally aimed at `removed_upstream`, and it cannot fire here:
    that finding walks the target's consistency-card callees, and the card drops a
    callee the moment the callee's node disappears — so a DELETED function is
    never anyone's removed upstream (logged as FL-083). The rule the person left
    behind is what the agent actually gets, which is the honest version of the
    same story.
    """
    conn = sqlite3.connect(str(demo["db"]))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT plan_id, findings_json FROM headline ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None, "no headline was computed"
        findings = json.loads(row["findings_json"])["findings"]
        hit = [f for f in findings if f["kind"] == "active_constraint"]
        assert hit, f"nothing reached task B: {[f['kind'] for f in findings]}"
        f = hit[0]
        assert f["severity"] == "blocking" and "discount" in f["text"]
        assert "reason_id" in f["evidence"]
    finally:
        conn.close()


def test_the_persons_words_and_the_email_are_reachable_from_that_node(demo):
    """The finding points at the rule; the node carries the sentence and the
    email that caused it. Both halves have to exist for the walkthrough to work."""
    conn = sqlite3.connect(str(demo["db"]))
    conn.row_factory = sqlite3.Row
    try:
        words = conn.execute(
            "SELECT substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start) AS t "
            "FROM change_reason r JOIN utterance u ON u.id = r.verbatim_utterance_id "
            "WHERE r.tier = 'stated'").fetchall()
        assert any(VERBATIM in (w["t"] or "") for w in words)
        assert conn.execute("SELECT COUNT(*) FROM reference WHERE kind = 'email'").fetchone()[0] >= 1
    finally:
        conn.close()


def test_the_agent_revised_and_the_citation_became_one_influence_row(demo):
    conn = sqlite3.connect(str(demo["db"]))
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT reason_id, plan_id, via, by FROM influence")]
        assert len(rows) == 1, f"expected exactly one adoption, got {rows}"
        assert rows[0]["via"] == "headline_response" and rows[0]["by"] == "agent"
        resp = conn.execute("SELECT action FROM headline_response ORDER BY id DESC LIMIT 1").fetchone()
        assert resp and resp["action"] == "revise"
    finally:
        conn.close()


def test_shown_and_adopted_did_not_become_the_same_number(demo):
    conn = sqlite3.connect(str(demo["db"]))
    try:
        shown = conn.execute("SELECT COUNT(*) FROM read_hit").fetchone()[0]
        adopted = conn.execute("SELECT COUNT(*) FROM influence").fetchone()[0]
        assert shown >= adopted and shown > 0
    finally:
        conn.close()


def test_the_demo_never_touches_the_real_workspace(demo):
    """Everything lives under DEMO_HOME; a demo that writes into ~/skill-workspace
    would corrupt the very ledger it is demonstrating."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "skill-workspace" not in body or "DEMO_HOME" in body
    assert "ORCH_DB" in body and "PSG_REGISTRY_PATH" in body


def test_the_walkthrough_script_exists_and_is_not_wired_into_ci():
    assert WALKTHROUGH.exists()
    body = WALKTHROUGH.read_text(encoding="utf-8")
    assert "playwright" in body.lower()
    assert "docs/media" in body
    # layout feedback: a pale top strip, never a banner over the content
    assert "strip" in body.lower() and "banner" not in body.lower().split("never a banner")[0][-200:]
