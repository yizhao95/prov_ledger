"""selfcheck: closes that could not write a git-note anchor (DP phase 3, Task 1).

A notes failure never blocks a plan from closing — spec §7 is explicit about
that. The price of "never blocks" is that the failure has nowhere to be seen,
so it is counted here: anchored, unanchored, and switched off, side by side.
"""
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from analyzer import cli  # noqa: E402
import selfcheck  # noqa: E402

from .test_session_trigger import _repo  # noqa: E402


def _orch(tmp_path, *logs):
    orch = tmp_path / "orch.db"
    c = sqlite3.connect(str(orch))
    c.executescript("CREATE TABLE Steps (step_id TEXT PRIMARY KEY, plan_id TEXT, status TEXT, "
                    "is_review TEXT, log_context TEXT);")
    for i, log in enumerate(logs):
        c.execute("INSERT INTO Steps (step_id, plan_id, status, is_review, log_context) VALUES (?, ?, ?, ?, ?)",
                  (f"p{i}-REVIEW", f"p{i}", "COMPLETED", "1", log))
    c.commit()
    c.close()
    return orch


def test_selfcheck_counts_the_closes_that_never_got_an_anchor(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    dbp = tmp_path / "g.db"
    cli.main([str(repo), "--project", "proj", "--db-path", str(dbp)])
    orch = _orch(tmp_path,
                 "[ANCHOR] chain heads anchored in refs/notes/provledger: note abc @ def",
                 "[ANCHOR] not anchored: no HEAD in /repos/x — p1 closed anyway",
                 "[ANCHOR] off: provledger-extensions.json says integrity.anchor=off",
                 "[ANCHOR] not anchored: git is not on PATH — p3 closed anyway")
    monkeypatch.setenv("ORCH_DB", str(orch))
    chk = next(x for x in selfcheck.run(str(dbp))["checks"] if x["name"] == "unanchored_closes")
    assert chk["severity"] == "warning" and chk["ok"] is True          # informational, never flips ok
    assert chk["count"] == 2 and chk["anchored"] == 1 and chk["switched_off"] == 1
    assert "2 close(s) could not write a git-note anchor" in chk["detail"]


def test_selfcheck_says_zero_when_there_is_no_orchestrator_db(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    dbp = tmp_path / "g.db"
    cli.main([str(repo), "--project", "proj", "--db-path", str(dbp)])
    monkeypatch.setenv("ORCH_DB", str(tmp_path / "missing.db"))
    chk = next(x for x in selfcheck.run(str(dbp))["checks"] if x["name"] == "unanchored_closes")
    assert chk["ok"] is True and "0 closes" in chk["detail"]
