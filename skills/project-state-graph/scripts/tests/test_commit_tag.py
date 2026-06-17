"""RED tests for git commit-SHA + dirty detection on an analysis run."""
import subprocess

from analyzer import cli, store


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("def f():\n    return 1\n")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_git_info_returns_sha_and_clean(tmp_path):
    repo = _make_repo(tmp_path)
    info = cli.git_info(str(repo))
    assert info["commit_sha"] and len(info["commit_sha"]) >= 7
    assert info["dirty"] is False


def test_git_info_detects_dirty(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "pkg" / "m.py").write_text("def f():\n    return 2  # changed\n")
    info = cli.git_info(str(repo))
    assert info["dirty"] is True


def test_run_records_commit_sha(tmp_path):
    repo = _make_repo(tmp_path)
    db_path = str(tmp_path / "demo-state-graph.db")
    cli.run(str(repo), "demo", db_path)
    conn = store.init_db(db_path)
    row = conn.execute(
        "SELECT commit_sha FROM analysis_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] is not None and len(row[0]) >= 7


def test_run_on_non_git_dir_is_graceful(tmp_path):
    plain = tmp_path / "plain"
    (plain / "pkg").mkdir(parents=True)
    (plain / "pkg" / "m.py").write_text("def f():\n    return 1\n")
    db_path = str(tmp_path / "plain-state-graph.db")
    # should not raise
    cli.run(str(plain), "plain", db_path)
    info = cli.git_info(str(plain))
    assert info["commit_sha"] is None
