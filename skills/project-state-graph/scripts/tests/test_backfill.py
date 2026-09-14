"""Phase 7 Task 3: analyzer backfill — replay past commits into a fresh graph.
A repo with 4 commits (add a function, rename it, move its file, delete it)
must yield one node whose history reads added -> renamed -> moved -> removed
across four backfill runs with increasing ids and trigger='backfill'."""
from __future__ import annotations

import io
import json
import sqlite3
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from analyzer import backfill, cli


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


BODY = "    y = x + 1\n    return y * 2\n"


@pytest.fixture
def repo4(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "other.py").write_text("def keep(z):\n    return z\n")
    _git(repo, "init", "-q"); _git(repo, "add", "."); _git(repo, "commit", "-qm", "c0 base")
    (repo / "pkg" / "m.py").write_text("def helper(x):\n" + BODY)
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "c1 add helper")
    (repo / "pkg" / "m.py").write_text("def helper_v2(x):\n" + BODY)
    _git(repo, "commit", "-qam", "c2 rename helper")
    _git(repo, "mv", "pkg/m.py", "pkg/n.py")
    _git(repo, "commit", "-qm", "c3 move file")
    (repo / "pkg" / "n.py").write_text("")
    _git(repo, "commit", "-qam", "c4 delete helper")
    shas = _git(repo, "rev-list", "--reverse", "HEAD").split()
    assert len(shas) == 5
    return repo, shas


def _runs(db):
    c = sqlite3.connect(str(db))
    return c.execute("SELECT id, commit_sha, trigger, plan_id, aborted, finished_at IS NOT NULL FROM analysis_run ORDER BY id").fetchall()


def test_backfill_replays_added_renamed_moved_removed(repo4, tmp_path):
    repo, shas = repo4
    db = tmp_path / "g.db"
    stats = backfill.run(str(repo), "bf", str(db), since=shas[1])
    assert stats["runs"] == 4 and stats["skipped"] == 0 and stats["sampled"] == 4 and stats["commits"] == 4
    runs = _runs(db)
    assert [r[1] for r in runs] == shas[1:] and all(r[2] == "backfill" and r[3] is None and r[4] == 0 and r[5] for r in runs)
    c = sqlite3.connect(str(db))
    key = c.execute("SELECT node_key FROM node_snapshot WHERE run_id=? AND qualified_name='pkg.m.helper'", (runs[0][0],)).fetchone()[0]
    ev = c.execute("SELECT run_id, event_type, payload_json FROM node_event WHERE node_key=? ORDER BY run_id, seq", (key,)).fetchall()
    types = [(r[0], r[1]) for r in ev if r[1] in ("node_added", "node_renamed", "node_moved", "node_removed")]
    assert [t for _, t in types] == ["node_added", "node_renamed", "node_renamed", "node_moved", "node_removed"] or \
           [t for _, t in types] == ["node_added", "node_renamed", "node_moved", "node_removed"], types
    assert [r for r, _ in types] == sorted(r for r, _ in types) and len({r for r, _ in types}) == 4
    renamed = [json.loads(r[2]) for r in ev if r[1] == "node_renamed"]
    assert renamed[0]["from"] == "pkg.m.helper" and renamed[0]["to"] == "pkg.m.helper_v2"
    moved = [json.loads(r[2]) for r in ev if r[1] == "node_moved"][0]
    assert (moved["from"], moved["to"]) == ("pkg/m.py", "pkg/n.py")
    assert stats["events"]["node_removed"] >= 1 and stats["ambiguous"] == 0
    # the history CLI reads it back with trigger=backfill on every line
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main(["history", str(db), key]) == 0
    out = buf.getvalue()
    assert out.count("trigger=backfill") >= 4 and "node_removed" in out and "plan=-" in out


def test_backfill_refuses_a_db_with_observed_history_unless_fresh(repo4, tmp_path):
    repo, shas = repo4
    db = tmp_path / "g.db"
    cli.run(str(repo), "bf", str(db), build_cards=False)                 # a real (live) run: keyed snapshots
    with pytest.raises(backfill.BackfillRefused, match="keyed snapshot"):
        backfill.run(str(repo), "bf", str(db), since=shas[1])
    assert len(_runs(db)) == 1                                          # nothing written
    stats = backfill.run(str(repo), "bf", str(db), since=shas[1], fresh_db=True)
    assert stats["runs"] == 4 and [r[2] for r in _runs(db)] == ["backfill"] * 4


def test_backfill_every_and_max_commits(repo4, tmp_path):
    repo, shas = repo4
    stats = backfill.run(str(repo), "bf", str(tmp_path / "a.db"), since=shas[1], every=2)
    assert stats["runs"] == 3 and [r[1] for r in _runs(tmp_path / "a.db")] == [shas[1], shas[3], shas[4]]   # sampled + the tip
    stats = backfill.run(str(repo), "bf", str(tmp_path / "b.db"), since=shas[1], every=2, max_commits=2)
    assert stats["runs"] == 2 and [r[1] for r in _runs(tmp_path / "b.db")] == [shas[1], shas[3]]
    stats = backfill.run(str(repo), "bf", str(tmp_path / "c.db"), since=shas[1], until=shas[2])
    assert stats["runs"] == 2 and [r[1] for r in _runs(tmp_path / "c.db")] == shas[1:3]


def test_backfill_resumes_without_duplicates(repo4, tmp_path):
    repo, shas = repo4
    db = tmp_path / "g.db"
    first = backfill.run(str(repo), "bf", str(db), since=shas[1], max_commits=2)
    assert first["runs"] == 2
    second = backfill.run(str(repo), "bf", str(db), since=shas[1])
    assert second["runs"] == 2 and second["skipped"] == 2
    runs = _runs(db)
    assert [r[1] for r in runs] == shas[1:] and len({r[1] for r in runs}) == 4
    assert not _git(repo, "worktree", "list").count("provledger-backfill")   # every worktree removed


def test_backfill_cli(repo4, tmp_path):
    repo, shas = repo4
    db = tmp_path / "g.db"
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["backfill", str(repo), "--project", "bf", "--db-path", str(db), "--since", shas[1], "--every", "2"])
    assert rc == 0 and "runs=3" in buf.getvalue() and "skipped=0" in buf.getvalue()
    with redirect_stdout(io.StringIO()):
        rc = cli.main(["backfill", str(repo), "--project", "bf", "--db-path", str(db), "--since", shas[1]])
    assert rc == 0 and len(_runs(db)) == 4                                # resumed: the missing commit only
    with pytest.raises(SystemExit):
        cli.main(["backfill", str(repo), "--project", "bf", "--db-path", str(db)])   # --since is required
