"""archive_db.sh keeps the newest five graph copies.

Every refresh copies the whole graph to `provledger.<sha>.db` beside it before
rebuilding it, and nothing removed the copies: a repository reviewed 144 times
held 19 GB of them. Nothing reads them; they are kept as raw material for a
future cross-version comparison. The script now keeps the newest five by mtime
and never touches the live graph or any other file.

The original checks of the script live in test_cold_archive.sh, which no suite
ran; the first test runs it.
"""
import os
import sqlite3
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARCHIVE = HERE.parent / "archive_db.sh"
KEEP = 5


def _graph(path: Path, sha: str) -> None:
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE analysis_run (id INTEGER PRIMARY KEY, commit_sha TEXT);"
                    f"INSERT INTO analysis_run (commit_sha) VALUES ('{sha}');")
    c.commit()
    c.close()


def test_the_scripts_own_checks_pass():
    r = subprocess.run(["bash", str(HERE / "test_cold_archive.sh")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_it_keeps_the_newest_five_copies_and_nothing_else_is_touched(tmp_path):
    live = tmp_path / "proj-state-graph.db"
    _graph(live, "sha_new")
    for i in range(7):
        old = tmp_path / f"provledger.sha_{i}.db"
        old.write_bytes(b"old graph")
        os.utime(old, (1_700_000_000 + i, 1_700_000_000 + i))
    other = tmp_path / "ARCHITECTURE.md"
    other.write_text("not an archive")
    r = subprocess.run(["bash", str(ARCHIVE), str(live)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    left = sorted(p.name for p in tmp_path.glob("provledger.*.db"))
    assert len(left) == KEEP, left
    assert "provledger.sha_new.db" in left, "the copy just made is the newest"
    assert left == sorted(["provledger.sha_new.db"] + [f"provledger.sha_{i}.db" for i in (3, 4, 5, 6)])
    assert live.exists() and other.read_text() == "not an archive"


def test_with_five_or_fewer_nothing_is_removed(tmp_path):
    live = tmp_path / "proj-state-graph.db"
    _graph(live, "sha_new")
    for i in range(3):
        (tmp_path / f"provledger.sha_{i}.db").write_bytes(b"old graph")
    subprocess.run(["bash", str(ARCHIVE), str(live)], capture_output=True, text=True, timeout=60, check=True)
    assert len(list(tmp_path.glob("provledger.*.db"))) == 4
