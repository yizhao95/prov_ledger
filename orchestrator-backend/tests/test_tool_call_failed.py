"""FL-208: a failed tool call is a tool call, and it is the one worth seeing.

Claude Code fires PostToolUseFailure — not PostToolUse — when a tool call
fails, so until this the log held only the calls that went well.
"""
import io
import json
import sqlite3

import pytest

from orchestrator import hooks


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    monkeypatch.setenv("ORCH_DB", str(dbp))
    monkeypatch.setenv("PROVLEDGER_HOOK_ERRORS", str(tmp_path / "hook-errors.log"))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(tmp_path / "projects.json"))
    monkeypatch.delenv("PROVLEDGER_HEADLESS", raising=False)
    return dbp


def _fire(monkeypatch, event: str, payload: dict) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return hooks.main([event])


PAYLOAD = {"session_id": "s1", "cwd": "/tmp/x", "tool_name": "Bash",
           "tool_input": {"command": "pytest tests/test_x.py"}}


def _rows(dbp):
    return sqlite3.connect(str(dbp)).execute(
        "SELECT tool_name, command_head, failed FROM tool_call_log ORDER BY id").fetchall()


def test_a_failed_call_is_logged_and_marked(hook_env, monkeypatch):
    assert _fire(monkeypatch, "PostToolUseFailure", {**PAYLOAD, "error": "Exit code 1"}) == 0
    assert _rows(hook_env) == [("Bash", "pytest tests/test_x.py", 1)]


def test_a_successful_call_is_logged_unmarked(hook_env, monkeypatch):
    assert _fire(monkeypatch, "PostToolUse", PAYLOAD) == 0
    assert _rows(hook_env) == [("Bash", "pytest tests/test_x.py", 0)]


def test_the_failure_event_is_a_known_event():
    assert "PostToolUseFailure" in hooks.EVENTS


def test_a_headless_provledger_call_logs_neither(hook_env, monkeypatch):
    monkeypatch.setenv("PROVLEDGER_HEADLESS", "1")
    _fire(monkeypatch, "PostToolUse", PAYLOAD)
    _fire(monkeypatch, "PostToolUseFailure", {**PAYLOAD, "error": "Exit code 1"})
    assert not hook_env.exists() or not sqlite3.connect(str(hook_env)).execute(
        "SELECT name FROM sqlite_master WHERE name='tool_call_log'").fetchone() or _rows(hook_env) == []
