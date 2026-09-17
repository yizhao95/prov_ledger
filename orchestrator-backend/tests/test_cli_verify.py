"""`provledger verify` at the command line (DP phase 3, Task 0; spec §7, G1).

The exit code is part of the contract: 0 when the chains walk, 3 when one does
not. The anchor line is always printed — "not anchored" is an answer, silence
is not.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import provenance as pv

REPO = Path(__file__).resolve().parents[2]


def _env(db_path):
    return dict(os.environ, ORCH_DB=str(db_path), PYTHONPATH=str(REPO / "orchestrator-backend"))


def _run(db_path, *args, cwd=None):
    return subprocess.run([sys.executable, "-m", "orchestrator.cli", "verify", *args],
                          capture_output=True, text=True, env=_env(db_path), cwd=str(cwd or REPO))


def _seed(conn):
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0",
                            text="keep the weekly grain", occurred_at="2026-09-17 10:00:00")
    pv.insert_reference(conn, project="proj", kind="email", label="re: grain", uri="mail:1",
                        occurred_at="2026-09-17 09:00:00")
    rid = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_a", kind="technical",
                           verbatim=(u, 0, 4), recorded_by="human")
    conn.commit()
    return rid


def _db_path(conn):
    return conn.execute("PRAGMA database_list").fetchone()[2]


def test_cli_verify_walks_the_chains_and_exits_zero(conn):
    _seed(conn)
    r = _run(_db_path(conn))
    assert r.returncode == 0, r.stderr
    assert "change_reason" in r.stdout and "ok" in r.stdout
    assert "not anchored" in r.stdout          # the anchor question is answered even when unasked


def test_cli_verify_json_is_the_report(conn):
    _seed(conn)
    r = _run(_db_path(conn), "--json")
    assert r.returncode == 0, r.stderr
    rep = json.loads(r.stdout)
    assert rep["ok"] is True and set(rep["tables"]) == {"utterance", "reference", "change_reason"}
    assert rep["anchored"] is False


def test_cli_verify_exits_three_on_a_broken_chain(conn, tmp_path):
    rid = _seed(conn)
    dst = tmp_path / "tampered.db"
    shutil.copy(_db_path(conn), dst)
    c = sqlite3.connect(dst)
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='change_reason'").fetchall():
        c.execute(f"DROP TRIGGER {name}")
    c.execute("UPDATE change_reason SET interpretation = 'quietly rewritten' WHERE id = ?", (rid,))
    c.commit()
    c.close()
    r = _run(dst)
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert f"chain broken at #{rid}" in r.stdout and "change_reason" in r.stdout


def test_cli_verify_against_notes_without_notes_says_not_anchored(conn, tmp_path):
    _seed(conn)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=str(repo), check=True, capture_output=True)
    r = _run(_db_path(conn), "--against-notes", "--repo", str(repo))
    assert r.returncode == 0, r.stderr
    assert "0 anchor(s)" in r.stdout and "not anchored" in r.stdout
