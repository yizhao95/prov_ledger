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


# ── DP phase 1 Task 3: UserPromptSubmit records the user's words verbatim ────
@pytest.fixture
def registry(tmp_path, monkeypatch):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [
        {"name": "outer", "repo": "/home/x/repo", "db_path": str(tmp_path / "o.db"), "commit_sha": "c"},
        {"name": "inner", "repo": "/home/x/repo/sub", "db_path": str(tmp_path / "i.db"), "commit_sha": "c"}]}))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(p))
    return p


def test_user_prompt_submit_records_the_prompt_verbatim_with_project_and_plan(hook_env, registry, monkeypatch, capsys):
    dbp, errlog = hook_env
    c = db.open_db(dbp); db.run_migrations(c)
    c.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) VALUES "
              "('older', 'g', 'IN_PROGRESS', 'outer', 'declared', '2026-09-15 09:00:00')")
    c.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) VALUES "
              "('newer', 'g', 'IN_PROGRESS', 'outer', 'declared', '2026-09-15 10:00:00')")
    c.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) VALUES "
              "('done', 'g', 'COMPLETED', 'outer', 'declared', '2026-09-15 11:00:00')")
    c.commit(); c.close()
    text = "  please keep fiscal weeks — 不要改成自然周\n第二行 "
    _feed(monkeypatch, {"session_id": "s9", "cwd": "/home/x/repo/pkg", "hook_event_name": "UserPromptSubmit", "prompt": text})
    assert hooks.main(["UserPromptSubmit"]) == 0 and capsys.readouterr().out == ""
    rows = sqlite3.connect(str(dbp)).execute("SELECT session_id, project, plan_id, text, visibility, occurred_at FROM utterance").fetchall()
    assert len(rows) == 1
    sid, project, plan_id, stored, vis, at = rows[0]
    assert (sid, project, plan_id, vis) == ("s9", "outer", "newer", "personal") and stored == text
    assert len(at) == 19 and not errlog.exists()


@pytest.mark.parametrize("prompt", ["", "   ", "/provledger-dashboard", "  /clear"])
def test_empty_prompts_and_slash_commands_are_not_recorded(hook_env, registry, monkeypatch, capsys, prompt):
    dbp, errlog = hook_env
    _feed(monkeypatch, {"session_id": "s", "cwd": "/home/x/repo", "hook_event_name": "UserPromptSubmit", "prompt": prompt})
    assert hooks.main(["UserPromptSubmit"]) == 0 and capsys.readouterr().out == ""
    assert sqlite3.connect(str(dbp)).execute("SELECT COUNT(*) FROM utterance").fetchone()[0] == 0


def test_cwd_outside_every_repo_still_records_with_project_null(hook_env, registry, monkeypatch, capsys):
    dbp, _ = hook_env
    _feed(monkeypatch, {"session_id": "s", "cwd": "/somewhere/else", "hook_event_name": "UserPromptSubmit", "prompt": "hello there"})
    assert hooks.main(["UserPromptSubmit"]) == 0 and capsys.readouterr().out == ""
    assert sqlite3.connect(str(dbp)).execute("SELECT project, plan_id, text FROM utterance").fetchall() == [(None, None, "hello there")]


def test_user_prompt_submit_on_an_unwritable_db_exits_zero_and_logs(hook_env, monkeypatch, capsys, tmp_path):
    dbp, errlog = hook_env
    monkeypatch.setenv("ORCH_DB", str(tmp_path / "no-such-dir" / "x" / "orch.db"))
    monkeypatch.setattr("orchestrator.db.open_db", lambda path: (_ for _ in ()).throw(sqlite3.OperationalError("unable to open database file")))
    _feed(monkeypatch, {"session_id": "s", "cwd": "/x", "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    assert hooks.main(["UserPromptSubmit"]) == 0 and capsys.readouterr().out == ""
    lines = errlog.read_text().splitlines()
    assert len(lines) == 1 and "UserPromptSubmit" in lines[0] and "OperationalError" in lines[0]


def test_project_for_cwd_longest_prefix(registry):
    from orchestrator import psg_bridge
    assert psg_bridge.project_for_cwd("/home/x/repo") == "outer"
    assert psg_bridge.project_for_cwd("/home/x/repo/pkg/m") == "outer"
    assert psg_bridge.project_for_cwd("/home/x/repo/sub/deeper") == "inner"
    assert psg_bridge.project_for_cwd("/home/x/repository") is None
    assert psg_bridge.project_for_cwd(None) is None and psg_bridge.project_for_cwd("") is None
