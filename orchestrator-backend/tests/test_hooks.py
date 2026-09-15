"""orchestrator.hooks — the Claude Code hook entry points (DP phase 0: PostToolUse).
A hook process never writes stdout, never exits non-zero; failures go to the
error log so selfcheck can count them."""
import io
import json
import sqlite3
import threading
import time

import pytest

from orchestrator import db, hooks


def _feed(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    errlog = tmp_path / "hook-errors.log"
    monkeypatch.setenv("ORCH_DB", str(dbp))
    monkeypatch.setenv("PROVLEDGER_HOOK_ERRORS", str(errlog))
    return dbp, errlog


def test_post_tool_use_inserts_a_tool_call_row_and_stdout_stays_empty(hook_env, monkeypatch, capsys):
    dbp, errlog = hook_env
    _feed(monkeypatch, {"session_id": "s1", "cwd": "/home/x/repo", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                        "tool_input": {"command": "ls"}, "tool_response": "..."})
    assert hooks.main(["PostToolUse"]) == 0
    assert capsys.readouterr().out == ""
    rows = sqlite3.connect(str(dbp)).execute("SELECT session_id, cwd, tool_name FROM tool_call_log").fetchall()
    assert rows == [("s1", "/home/x/repo", "Bash")]
    assert not errlog.exists()


def test_tool_call_log_is_append_only(conn):
    conn.execute("INSERT INTO tool_call_log (session_id, cwd, tool_name) VALUES ('s', '/x', 'Read')")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE tool_call_log SET tool_name='Write'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM tool_call_log")
    at = conn.execute("SELECT at FROM tool_call_log").fetchone()[0]
    assert len(at) == 23 and at[19] == "."          # millisecond default: 'YYYY-MM-DD HH:MM:SS.mmm'


def test_hook_on_a_locked_db_exits_zero_and_logs_one_line(hook_env, monkeypatch, capsys):
    dbp, errlog = hook_env
    c = db.open_db(dbp); db.run_migrations(c)
    c.execute("BEGIN EXCLUSIVE")                       # hold the write lock past the hook's busy_timeout
    t0 = time.monotonic()
    _feed(monkeypatch, {"session_id": "s1", "cwd": "/x", "hook_event_name": "PostToolUse", "tool_name": "Bash"})
    assert hooks.main(["PostToolUse"]) == 0
    assert capsys.readouterr().out == ""
    assert time.monotonic() - t0 < 10
    c.rollback(); c.close()
    lines = errlog.read_text().splitlines()
    assert len(lines) == 1 and "PostToolUse" in lines[0] and "OperationalError" in lines[0]


def test_garbage_stdin_and_unknown_event_never_raise(hook_env, monkeypatch, capsys):
    dbp, errlog = hook_env
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hooks.main(["PostToolUse"]) == 0 and capsys.readouterr().out == ""
    assert errlog.exists() and "ValueError" in errlog.read_text() or "JSONDecodeError" in errlog.read_text()
    _feed(monkeypatch, {"session_id": "s1"})
    assert hooks.main(["SomethingElse"]) == 0 and capsys.readouterr().out == ""
