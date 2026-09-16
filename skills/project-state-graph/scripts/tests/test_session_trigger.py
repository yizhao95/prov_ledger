"""DP phase 2 (Task 7b): `analyzer … --trigger session --session-id S` attributes the run
to the placeholder plan session:<S> and records the session id; selfcheck counts
sessions without a plan from the orchestrator DB."""
import json
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from analyzer import cli  # noqa: E402
import selfcheck  # noqa: E402


def _repo(tmp_path):
    repo = tmp_path / "repo"; (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "m.py").write_text("import pandas as pd\n\n\ndef load_orders(df):\n    return df[df.paid]\n")
    return repo


def test_session_trigger_sets_plan_id_placeholder_and_session_id(tmp_path):
    repo = _repo(tmp_path); dbp = tmp_path / "g.db"
    assert cli.main([str(repo), "--project", "proj", "--db-path", str(dbp), "--trigger", "session", "--session-id", "sess-9"]) == 0
    c = sqlite3.connect(str(dbp))
    trigger, plan_id, ext = c.execute("SELECT trigger, plan_id, extensions_json FROM analysis_run ORDER BY id DESC LIMIT 1").fetchone()
    assert trigger == "session" and plan_id == "session:sess-9" and json.loads(ext)["session_id"] == "sess-9"
    # an explicit plan id still wins; without --session-id nothing changes
    assert cli.main([str(repo), "--project", "proj", "--db-path", str(dbp), "--trigger", "session", "--session-id", "sess-9", "--plan-id", "P1"]) == 0
    assert c.execute("SELECT plan_id FROM analysis_run ORDER BY id DESC LIMIT 1").fetchone()[0] == "P1"
    assert cli.main([str(repo), "--project", "proj", "--db-path", str(dbp)]) == 0
    trigger, plan_id, ext = c.execute("SELECT trigger, plan_id, extensions_json FROM analysis_run ORDER BY id DESC LIMIT 1").fetchone()
    assert trigger == "manual" and plan_id is None and (ext is None or "session_id" not in json.loads(ext))


def test_selfcheck_counts_sessions_without_plan_from_the_orchestrator_db(tmp_path, monkeypatch):
    repo = _repo(tmp_path); dbp = tmp_path / "g.db"
    cli.main([str(repo), "--project", "proj", "--db-path", str(dbp)])
    orch = tmp_path / "orch.db"
    c = sqlite3.connect(str(orch))
    c.executescript("""
        CREATE TABLE session_run (session_id TEXT PRIMARY KEY, project TEXT, cwd TEXT, started_at TEXT, ended_at TEXT, psg_run_id INTEGER, refresh_state TEXT NOT NULL DEFAULT 'skipped', note TEXT);
        CREATE TABLE change_reason (id INTEGER PRIMARY KEY, plan_id TEXT, role TEXT, tier TEXT);
        INSERT INTO session_run (session_id, project, refresh_state, note) VALUES ('s1', 'proj', 'done', 'refresh queued (pid 1): tracked files changed');
        INSERT INTO session_run (session_id, project, refresh_state, note) VALUES ('s2', 'proj', 'skipped', 'plans in session: P1 — the close already refreshed');
        INSERT INTO session_run (session_id, project, refresh_state, note) VALUES ('s3', 'proj', 'skipped', 'tree unchanged since the graph was built');
        INSERT INTO change_reason (plan_id, role, tier) VALUES ('session:s1', 'reason', 'unstated'), ('session:s1', 'reason', 'derived'), ('P1', 'reason', 'asserted');
    """)
    c.commit(); c.close()
    monkeypatch.setenv("ORCH_DB", str(orch))
    res = selfcheck.run(str(dbp))
    chk = next(x for x in res["checks"] if x["name"] == "sessions_without_plan")
    assert chk["ok"] is True and chk["severity"] == "warning" and chk["count"] == 2 and chk["unstated"] == 1 and chk["slots"] == 2
    assert "2 session(s) without a plan" in chk["detail"] and "1/2 unstated" in chk["detail"]
    monkeypatch.setenv("ORCH_DB", str(tmp_path / "missing.db"))
    chk = next(x for x in selfcheck.run(str(dbp))["checks"] if x["name"] == "sessions_without_plan")
    assert chk["ok"] is True and "0 sessions" in chk["detail"]
