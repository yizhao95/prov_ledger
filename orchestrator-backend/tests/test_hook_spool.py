"""A hook that cannot get the write lock spools its row; the next one replays it (FL-193).

Before this a hook waited out its busy timeout, logged "database is locked" and
exited 0 — and the user's words or the tool call were gone. WAL removes the
readers from the way, but a writer still waits behind another writer (a plan
close, another hook). So a UserPromptSubmit or PostToolUse that cannot write now
appends its payload and the time it happened to a spool file beside the ledger,
and the next hook that gets the lock replays the spool, in order and with the
original times, before writing its own row.

Stop is not spooled: replaying it later would also queue a graph refresh at an
arbitrary moment and stamp the session's end with the wrong time.
"""
import io
import json
import sqlite3

import pytest

from orchestrator import db, hooks


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "orchestrator.db"
    monkeypatch.setenv("ORCH_DB", str(p))
    monkeypatch.setenv("PROVLEDGER_HOOK_ERRORS", str(tmp_path / "hook-errors.log"))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(tmp_path / "projects.json"))
    monkeypatch.delenv("PROVLEDGER_HEADLESS", raising=False)
    monkeypatch.setattr(hooks, "BUSY_TIMEOUT_MS", 100)
    conn = db.open_db(p)
    db.run_migrations(conn)
    conn.close()
    return p


def _fire(monkeypatch, event, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return hooks.main([event])


def _locked(path):
    """Another writer holding the write lock, as a plan close or a hook would."""
    c = sqlite3.connect(str(path), timeout=0.1)
    c.execute("BEGIN IMMEDIATE")
    return c


def _spool(path):
    return hooks.spool_path(path)


PROMPT = {"session_id": "s1", "cwd": "/w", "prompt": "keep the old discount column until the June release"}
TOOL = {"session_id": "s1", "cwd": "/w", "tool_name": "Bash", "tool_input": {"command": "pytest -q"}}


def _rows(path, sql):
    c = sqlite3.connect(str(path))
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_a_locked_ledger_spools_the_users_words_instead_of_dropping_them(ledger, monkeypatch):
    holder = _locked(ledger)
    assert _fire(monkeypatch, "UserPromptSubmit", PROMPT) == 0
    holder.rollback(); holder.close()
    assert _rows(ledger, "SELECT count(*) FROM utterance") == [(0,)]
    lines = _spool(ledger).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "UserPromptSubmit" and entry["data"]["prompt"] == PROMPT["prompt"]
    assert entry["occurred_at"]


def test_the_next_hook_replays_the_spool_in_order_with_the_original_times(ledger, monkeypatch):
    holder = _locked(ledger)
    _fire(monkeypatch, "UserPromptSubmit", PROMPT)
    _fire(monkeypatch, "PostToolUse", TOOL)
    _fire(monkeypatch, "PostToolUseFailure", {**TOOL, "tool_input": {"command": "false"}, "error": "Exit code 1"})
    holder.rollback(); holder.close()
    spooled = [json.loads(l) for l in _spool(ledger).read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in spooled] == ["UserPromptSubmit", "PostToolUse", "PostToolUseFailure"]

    _fire(monkeypatch, "PostToolUse", {**TOOL, "tool_input": {"command": "ls"}})   # gets the lock

    said = _rows(ledger, "SELECT text, occurred_at FROM utterance")
    assert said == [(PROMPT["prompt"], spooled[0]["occurred_at"])], "the words keep the time they were said"
    calls = _rows(ledger, "SELECT command_head, failed, at FROM tool_call_log ORDER BY id")
    assert [(c[0], c[1]) for c in calls] == [("pytest -q", 0), ("false", 1), ("ls", 0)], \
        "replayed first, in order, then the hook's own row"
    assert calls[0][2].startswith(spooled[1]["occurred_at"][:19]) and calls[1][2].startswith(spooled[2]["occurred_at"][:19])
    assert not _spool(ledger).exists() or _spool(ledger).read_text(encoding="utf-8") == ""


def test_a_corrupt_spool_line_is_skipped_and_logged_and_the_rest_replayed(ledger, monkeypatch, tmp_path):
    holder = _locked(ledger)
    _fire(monkeypatch, "PostToolUse", TOOL)
    holder.rollback(); holder.close()
    with _spool(ledger).open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    _fire(monkeypatch, "PostToolUse", {**TOOL, "tool_input": {"command": "ls"}})
    assert [r[0] for r in _rows(ledger, "SELECT command_head FROM tool_call_log ORDER BY id")] == ["pytest -q", "ls"]
    assert "spool" in (tmp_path / "hook-errors.log").read_text(encoding="utf-8")


def test_stop_is_not_spooled(ledger, monkeypatch):
    holder = _locked(ledger)
    assert _fire(monkeypatch, "Stop", {"session_id": "s1", "cwd": "/w"}) == 0
    holder.rollback(); holder.close()
    assert not _spool(ledger).exists()
