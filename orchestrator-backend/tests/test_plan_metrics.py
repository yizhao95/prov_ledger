"""orchestrator.plan_metrics — what a plan cost in tool calls (DP phase 0, H1)."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from _live import require_live_ledger
from orchestrator import db, plan_metrics

REPO = Path(__file__).resolve().parents[2]


def _plan(conn, plan_id, created, completed, project="proj", steps=3, log_entries=0):
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, created_at, completed_at, project, project_source) "
                 "VALUES (?, 'g', 'COMPLETED', ?, ?, ?, 'declared')", (plan_id, created, completed, project))
    for i in range(steps):
        log = "\n---\n".join(f"entry {k}" for k in range(log_entries)) if log_entries else ""
        conn.execute("INSERT INTO Steps (step_id, plan_id, description, status, execution_order, depth_level, log_context, step_type) "
                     "VALUES (?, ?, 'd', 'COMPLETED', ?, 0, ?, 'COMMAND')", (f"{plan_id}-{i}", plan_id, i, log))
    conn.commit()


def _call(conn, session, cwd, tool, at):
    conn.execute("INSERT INTO tool_call_log (session_id, cwd, tool_name, at) VALUES (?, ?, ?, ?)", (session, cwd, tool, at))
    conn.commit()


@pytest.fixture
def registry(tmp_path, monkeypatch):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": "proj", "repo": "/home/x/repo", "db_path": str(tmp_path / "g.db"), "commit_sha": "c"}]}))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(p))
    return p


def test_calls_for_plan_counts_only_the_plan_window_and_the_project_repo(conn, registry):
    _plan(conn, "P1", "2026-09-15 10:00:00", "2026-09-15 10:10:00", steps=2)
    _call(conn, "s", "/home/x/repo", "Bash", "2026-09-15 10:01:00.000")          # in
    _call(conn, "s", "/home/x/repo/sub", "Read", "2026-09-15 10:05:00.500")      # in (subdir of the repo)
    _call(conn, "s", "/home/x/other", "Bash", "2026-09-15 10:06:00.000")         # other repo
    _call(conn, "s", "/home/x/repo", "Bash", "2026-09-15 09:59:59.999")          # before
    _call(conn, "s", "/home/x/repo", "Bash", "2026-09-15 10:10:00.001")          # after
    m = plan_metrics.calls_for_plan(conn, "P1")
    assert m["plan_id"] == "P1" and m["tool_calls"] == 2 and m["steps"] == 2 and m["calls_per_step"] == 1.0
    assert m["wall_s"] == 600 and m["measured"] is True and m["wall_per_step"] == 300.0


def test_calls_for_plan_without_a_project_uses_the_window_only_and_history_uses_a_proxy(conn):
    _plan(conn, "P2", "2026-09-15 11:00:00", "2026-09-15 11:01:00", project=None, steps=2, log_entries=4)
    _call(conn, "s", "/anywhere", "Bash", "2026-09-15 11:00:30.000")
    m = plan_metrics.calls_for_plan(conn, "P2")
    assert m["tool_calls"] == 1 and m["measured"] is True
    _plan(conn, "P3", "2026-09-14 11:00:00", "2026-09-14 11:02:00", project=None, steps=2, log_entries=4)
    m3 = plan_metrics.calls_for_plan(conn, "P3")
    assert m3["tool_calls"] == 0 and m3["measured"] is False and m3["calls_per_step"] is None
    assert m3["proxy_log_entries"] == 8 and m3["proxy_calls_per_step"] == 4.0     # never mixed into calls_per_step


def test_review_steps_do_not_count_as_steps(conn):
    _plan(conn, "P4", "2026-09-15 12:00:00", "2026-09-15 12:01:00", project=None, steps=2)
    conn.execute("INSERT INTO Steps (step_id, plan_id, description, status, execution_order, depth_level, is_review) "
                 "VALUES ('P4-REVIEW', 'P4', 'r', 'COMPLETED', 9, 0, 1)")
    conn.commit()
    assert plan_metrics.calls_for_plan(conn, "P4")["steps"] == 2


def test_baseline_median_and_p90_over_completed_plans(conn):
    for i, n in enumerate([2, 4, 6, 8, 10]):          # calls per plan, 2 steps each → cps 1,2,3,4,5
        pid = f"B{i}"
        _plan(conn, pid, f"2026-09-15 0{i}:00:00", f"2026-09-15 0{i}:10:00", project=None, steps=2, log_entries=1)
        for k in range(n):
            _call(conn, "s", "/x", "Bash", f"2026-09-15 0{i}:0{k % 10}:00.000")
    _plan(conn, "H1", "2026-09-10 01:00:00", "2026-09-10 01:10:00", project=None, steps=2, log_entries=3)   # history: proxy only
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, created_at) VALUES ('open', 'g', 'IN_PROGRESS', '2026-09-15 05:00:00')")
    conn.commit()
    b = plan_metrics.baseline(conn)
    assert b["n_measured"] == 5 and b["n_proxy"] == 1 and b["n_plans"] == 6
    assert b["calls_per_step"]["median"] == 3.0 and b["calls_per_step"]["p90"] == pytest.approx(4.6)
    assert b["wall_per_step"]["median"] == 300.0
    assert b["proxy_calls_per_step"]["median"] == 3.0 and "generated_at" in b
    since = plan_metrics.baseline(conn, since="2026-09-15 03:00:00")
    assert since["n_measured"] == 2 and since["n_proxy"] == 0


def test_baseline_file_round_trip(tmp_path, conn):
    _plan(conn, "P1", "2026-09-15 10:00:00", "2026-09-15 10:10:00", project=None, steps=1)
    _call(conn, "s", "/x", "Bash", "2026-09-15 10:01:00.000")
    data = plan_metrics.baseline(conn)
    p = tmp_path / "perf-baseline.json"
    plan_metrics.write_baseline(p, data)
    back = plan_metrics.read_baseline(p)
    assert back == data and back["calls_per_step"]["median"] == 1.0
    assert plan_metrics.read_baseline(tmp_path / "missing.json") is None


def test_cli_metrics_plan_prints_json(tmp_path, conn):
    _plan(conn, "P1", "2026-09-15 10:00:00", "2026-09-15 10:10:00", project=None, steps=1)
    _call(conn, "s", "/x", "Bash", "2026-09-15 10:01:00.000")
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(REPO / "orchestrator-backend"))
    r = subprocess.run([sys.executable, "-m", "orchestrator.cli", "metrics", "plan", "P1"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["tool_calls"] == 1
    r = subprocess.run([sys.executable, "-m", "orchestrator.cli", "metrics", "baseline", "--write", str(tmp_path / "b.json")],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and json.loads((tmp_path / "b.json").read_text())["n_measured"] == 1


@pytest.mark.live
def test_h1_threshold():
    """H1: the tool must not silently get slower — the last 5 measured plans of
    this repository stay within 1.5 × the baseline's p90 calls per step.

    DP phase 2c marked this `live`: it reads the dogfood ledger, so the same
    commit passes or fails depending on what ran on this machine. The threshold
    LOGIC is asserted on fixtures in test_live_marker.py; this one is the real
    reading, and you ask for it by name."""
    require_live_ledger("H1")
    base = plan_metrics.read_baseline(REPO / "docs" / "perf-baseline.json")
    if base is None:
        pytest.skip("perf baseline not measured yet — run `python -m orchestrator.cli metrics baseline --write docs/perf-baseline.json`")
    live = Path(os.environ.get("ORCH_DB") or (Path.home() / "skill-workspace" / "orchestrator.db"))
    if not live.exists():
        pytest.skip(f"no orchestrator DB at {live} — H1 needs the dogfood database")
    c = db.open_db(live)
    try:
        recent = [plan_metrics.calls_for_plan(c, r[0]) for r in c.execute(
            "SELECT plan_id FROM Plans WHERE status='COMPLETED' ORDER BY created_at DESC LIMIT 25")]
    finally:
        c.close()
    measured = [m for m in recent if m["measured"] and m["calls_per_step"] is not None][:5]
    if not measured:
        pytest.skip("no measured plan yet (tool_call_log is empty for the recent plans) — the PostToolUse hook has not counted a plan")
    # DP phase 2d: this line used to raise TypeError the first time a session's
    # hooks were actually live. H1 had SKIPPED since phase 0 because no plan was
    # ever measured (FL-051: the session that installs the hooks does not count
    # itself), so nobody noticed that the baseline it compares against was
    # written from a hook-less run and carries p90 = null. A missing baseline is
    # a thing to SAY, not to crash on.
    p90 = (base.get("calls_per_step") or {}).get("p90")
    if p90 is None:
        pytest.skip("the baseline carries no measured calls_per_step p90 — regenerate it with "
                    "`python -m orchestrator.cli metrics baseline --write docs/perf-baseline.json` "
                    "from a session whose hooks are live")
    limit = 1.5 * p90
    over = [(m["plan_id"], m["calls_per_step"]) for m in measured if m["calls_per_step"] > limit]
    assert not over, f"calls_per_step above 1.5 × baseline p90 ({limit:.2f}): {over}"
