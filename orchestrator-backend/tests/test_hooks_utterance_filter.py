"""DP phase 2d (Task 0): a UserPromptSubmit payload that Claude Code injected is
not something the user said.

`<task-notification>`, `<system-reminder>`, `<local-command-caveat>` and
`<command-name>` prompts are harness text; recording them as utterances makes
them R0 candidates for a `stated` reason — which would let the system quote
itself as the user (north star: "never go silent" cuts both ways; the ledger must not
invent a speaker). Utterance #28/#29 of the 2d session were exactly that.
"""
import io
import os
import json
import sqlite3

import pytest

from orchestrator import db, hooks

INJECTED_PREFIXES = ("<task-notification>", "<system-reminder>",
                     "<local-command-caveat>", "<command-name>")


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    errlog = tmp_path / "hook-errors.log"
    monkeypatch.setenv("ORCH_DB", str(dbp))
    monkeypatch.setenv("PROVLEDGER_HOOK_ERRORS", str(errlog))
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "outer", "repo": "/home/x/repo",
                                             "db_path": str(tmp_path / "o.db"), "commit_sha": "c"}]}))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    return dbp, errlog


def _feed(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _utterances(dbp) -> list:
    return sqlite3.connect(str(dbp)).execute("SELECT text FROM utterance ORDER BY id").fetchall()


@pytest.mark.parametrize("prefix", INJECTED_PREFIXES)
def test_claude_code_injected_prompts_are_not_utterances(hook_env, monkeypatch, capsys, prefix):
    dbp, errlog = hook_env
    prompt = f"{prefix}\n<task-id>abc</task-id>\nthe harness talking, not the user\n"
    _feed(monkeypatch, {"session_id": "s", "cwd": "/home/x/repo", "hook_event_name": "UserPromptSubmit", "prompt": prompt})
    assert hooks.main(["UserPromptSubmit"]) == 0 and capsys.readouterr().out == ""
    assert _utterances(dbp) == [] and not errlog.exists()


@pytest.mark.parametrize("prefix", INJECTED_PREFIXES)
def test_leading_whitespace_does_not_smuggle_an_injected_prompt_in(hook_env, monkeypatch, capsys, prefix):
    dbp, _ = hook_env
    _feed(monkeypatch, {"session_id": "s", "cwd": "/home/x/repo", "hook_event_name": "UserPromptSubmit",
                        "prompt": f"\n  {prefix} still the harness"})
    assert hooks.main(["UserPromptSubmit"]) == 0 and capsys.readouterr().out == ""
    assert _utterances(dbp) == []


def test_record_utterance_returns_none_for_injected_text(hook_env):
    dbp, _ = hook_env
    conn = db.open_db(dbp)
    db.run_migrations(conn)
    for prefix in INJECTED_PREFIXES:
        assert hooks.record_utterance(conn, {"session_id": "s", "cwd": "/home/x/repo",
                                             "prompt": f"{prefix} body"}) is None
    conn.close()
    assert _utterances(dbp) == []


def test_a_real_prompt_that_merely_mentions_the_tags_is_still_recorded(hook_env, monkeypatch, capsys):
    """The filter is a PREFIX filter, never a substring filter — a user may well
    write about `<system-reminder>` and those words are still theirs."""
    dbp, errlog = hook_env
    text = "stop recording <system-reminder> as if it were something I said — filter on the prefix only"
    _feed(monkeypatch, {"session_id": "s", "cwd": "/home/x/repo", "hook_event_name": "UserPromptSubmit", "prompt": text})
    assert hooks.main(["UserPromptSubmit"]) == 0 and capsys.readouterr().out == ""
    assert _utterances(dbp) == [(text,)] and not errlog.exists()


def test_the_already_misrecorded_rows_are_kept_and_a_retraction_is_appended(tmp_path):
    """History only appends: migration 021 does not delete utterance #28/#29, it
    widens trigger_log.verdict and writes one `retract` row per injected row."""
    dbp = tmp_path / "orch.db"
    conn = db.open_db(dbp)
    db.run_migrations(conn)
    from orchestrator import provenance as pv
    good = pv.insert_utterance(conn, session_id="s", project="outer", plan_id=None,
                               text="keep fiscal weeks", occurred_at="2026-09-16 10:00:00")
    bad = pv.insert_utterance(conn, session_id="s", project="outer", plan_id=None,
                              text="<task-notification>\n<task-id>x</task-id>\n", occurred_at="2026-09-16 10:01:00")
    conn.commit()
    from orchestrator import provenance_migrate as pm
    n = pm.retract_injected_utterances(conn)
    assert n == 1
    kept = [r[0] for r in conn.execute("SELECT id FROM utterance ORDER BY id")]
    assert good in kept and bad in kept                      # nothing deleted
    rows = conn.execute("SELECT verdict, basis FROM trigger_log WHERE rule_id = 'utterance-filter'").fetchall()
    assert len(rows) == 1 and rows[0][0] == "retract" and str(bad) in rows[0][1]
    assert pm.retract_injected_utterances(conn) == 0         # idempotent
    conn.close()


# FL-182: a subagent's report reaches the parent session through UserPromptSubmit
# as an `<agent-message …>` block. On 2026-10-03 eleven of them were recorded as
# the user's words in one session, and R0 then filed 75 `stated` reasons on them.
AGENT_MESSAGE = ('<agent-message from="ac44f387dd59e9a92">\n[Subagent hand-back] The text below is the '
                 'final report of a subagent this session delegated to.\n  ## Report\n  the subagent '
                 'emailed nobody; it read the code and rewrote the README\n</agent-message>\n')


def test_a_subagent_report_is_not_an_utterance(hook_env, monkeypatch, capsys):
    dbp, errlog = hook_env
    _feed(monkeypatch, {"session_id": "s", "cwd": "/home/x/repo", "hook_event_name": "UserPromptSubmit",
                        "prompt": AGENT_MESSAGE})
    assert hooks.main(["UserPromptSubmit"]) == 0
    assert capsys.readouterr().out == "", "no source hint for words the user never said"
    assert _utterances(dbp) == [] and not errlog.exists()


def test_a_user_who_writes_about_agent_messages_is_still_recorded(hook_env, monkeypatch, capsys):
    dbp, _ = hook_env
    text = "why did the <agent-message> blocks end up in the ledger as my words?"
    _feed(monkeypatch, {"session_id": "s", "cwd": "/home/x/repo", "hook_event_name": "UserPromptSubmit", "prompt": text})
    assert hooks.main(["UserPromptSubmit"]) == 0
    assert _utterances(dbp) == [(text,)]


# FL-182, the other half: provLedger's own headless `claude -p` calls (the judge,
# the arbiter, ask's summary) run with the user's plugins on, so this very hook
# recorded provLedger's prompts as the user's words. The runner marks its child
# with PROVLEDGER_HEADLESS=1, and every hook stands down under it.
def test_a_headless_provledger_call_records_nothing(hook_env, monkeypatch, capsys):
    dbp, errlog = hook_env
    monkeypatch.setenv("PROVLEDGER_HEADLESS", "1")
    _feed(monkeypatch, {"session_id": "s", "cwd": "/home/x/repo", "hook_event_name": "UserPromptSubmit",
                        "prompt": "You judge whether ONE record answers the question. Sarah emailed it."})
    assert hooks.main(["UserPromptSubmit"]) == 0 and capsys.readouterr().out == ""
    _feed(monkeypatch, {"session_id": "s", "cwd": "/home/x/repo", "hook_event_name": "PostToolUse",
                        "tool_name": "Bash", "tool_input": {"command": "true"}})
    assert hooks.main(["PostToolUse"]) == 0
    conn = sqlite3.connect(str(dbp))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "utterance" not in tables or _utterances(dbp) == []
    if "tool_call_log" in tables:
        assert conn.execute("SELECT count(*) FROM tool_call_log").fetchone()[0] == 0
    assert not errlog.exists()


def test_the_headless_runner_marks_its_child(tmp_path, monkeypatch):
    from orchestrator.testing import claude_arbiter
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\ncat >/dev/null\nprintf \'{"result": "headless=%s"}\' "$PROVLEDGER_HEADLESS"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.delenv("PROVLEDGER_HEADLESS", raising=False)
    text, _detail = claude_arbiter.default_runner("hello", timeout_s=20)
    assert text == "headless=1"
