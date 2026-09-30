"""A1: which door the words came in through.

`recorded_by` is what the writer says about itself — and `provledger note`
hardcodes `"human"`, so an agent that runs that command produces a row claiming a
person typed it. `origin` is decided by the entry point instead of declared by
the writer, which makes it the half of "who said this" that can be checked.

Three doors matter here:

  hook       the UserPromptSubmit hook, the only path that can show the words
             were captured as they were typed
  human_cli  `provledger note` run by a person at a terminal
  agent_cli  the same command run by an agent

`provledger note` is deliberately still allowed to produce `stated` — people do
need to record something after the fact. What must not happen is the two becoming
indistinguishable, so the origin travels with the record into `change_reason_v`.

The failure direction is fixed: when the terminal cannot be identified the row
says `agent_cli`. Guessing `human_cli` would manufacture exactly the claim this
column exists to check.
"""
import io
import json
import sqlite3
from argparse import Namespace

import pytest

from orchestrator import cli, db, hooks, provenance as pv


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    monkeypatch.setenv("ORCH_DB", str(dbp))
    monkeypatch.setenv("PROVLEDGER_HOOK_ERRORS", str(tmp_path / "hook-errors.log"))
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "proj", "repo": "/home/x/repo",
                                             "db_path": str(tmp_path / "g.db"), "commit_sha": "c"}]}))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    return dbp


def _origins(dbp) -> list:
    return [r[0] for r in sqlite3.connect(str(dbp)).execute("SELECT origin FROM utterance ORDER BY id")]


def _note_args(**over) -> Namespace:
    args = Namespace(text="finance asked for fiscal weeks", at="2026-09-15 14:30", project="proj",
                     plan=None, node=None, kind="organizational", ref=[], session="t")
    for k, v in over.items():
        setattr(args, k, v)
    return args


class _Stdin(io.StringIO):
    """A stdin whose isatty() can be made to answer, to lie, or to be absent."""

    def __init__(self, isatty):
        super().__init__("")
        self._isatty = isatty

    def isatty(self):
        return self._isatty()


class _NoIsatty:
    """Some wrappers (a pipe replaced by a bare object, a closed stream) have no
    isatty at all. Attribute lookup must not be what decides provenance."""


# ── (a) the hook ────────────────────────────────────────────────────────────

def test_the_hook_records_that_it_captured_the_words_as_they_were_typed(hook_env):
    dbp = hook_env
    conn = db.open_db(dbp)
    db.run_migrations(conn)
    uid = hooks.record_utterance(conn, {"session_id": "s", "cwd": "/home/x/repo",
                                        "prompt": "please keep fiscal weeks"})
    conn.close()
    assert uid is not None
    assert _origins(dbp) == ["hook"]


def test_the_hook_main_path_records_the_same_origin(hook_env, monkeypatch, capsys):
    """Through main(), not just the helper: the entry point is the thing being
    claimed, so the claim has to hold at the entry point."""
    dbp = hook_env
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "s", "cwd": "/home/x/repo", "hook_event_name": "UserPromptSubmit",
         "prompt": "do not switch to calendar weeks"})))
    assert hooks.main(["UserPromptSubmit"]) == 0
    capsys.readouterr()
    assert _origins(dbp) == ["hook"]


# ── (b, c) the CLI reads the door it was opened through ─────────────────────

def test_note_from_a_pipe_is_recorded_as_an_agent(hook_env, monkeypatch):
    """stdin is not a terminal: something was driving the command, not someone."""
    dbp = hook_env
    monkeypatch.setattr("sys.stdin", _Stdin(lambda: False))
    assert cli._note(_note_args()) == 0
    assert _origins(dbp) == ["agent_cli"]


def test_note_from_a_terminal_is_recorded_as_a_person(hook_env, monkeypatch):
    dbp = hook_env
    monkeypatch.setattr("sys.stdin", _Stdin(lambda: True))
    assert cli._note(_note_args()) == 0
    assert _origins(dbp) == ["human_cli"]


# ── (d) the failure direction is the safe one ───────────────────────────────

def test_an_isatty_that_raises_is_recorded_as_an_agent(hook_env, monkeypatch):
    dbp = hook_env
    monkeypatch.setattr("sys.stdin", _Stdin(lambda: (_ for _ in ()).throw(ValueError("I/O operation on closed file"))))
    assert cli._note(_note_args()) == 0
    assert _origins(dbp) == ["agent_cli"], "an unanswerable question is never answered with 'a person'"


def test_a_stdin_without_isatty_is_recorded_as_an_agent(hook_env, monkeypatch):
    dbp = hook_env
    monkeypatch.setattr("sys.stdin", _NoIsatty())
    assert cli._note(_note_args()) == 0
    assert _origins(dbp) == ["agent_cli"]


def test_stdin_replaced_by_none_is_recorded_as_an_agent(hook_env, monkeypatch):
    """pythonw / a detached process leaves sys.stdin as None."""
    dbp = hook_env
    monkeypatch.setattr("sys.stdin", None)
    assert cli._note(_note_args()) == 0
    assert _origins(dbp) == ["agent_cli"]


@pytest.mark.parametrize("stdin", [_Stdin(lambda: False), _NoIsatty(), None])
def test_human_cli_is_never_the_fallback(hook_env, monkeypatch, stdin):
    dbp = hook_env
    monkeypatch.setattr("sys.stdin", stdin)
    cli._note(_note_args())
    assert "human_cli" not in _origins(dbp)


# ── (e) still stated, and still distinguishable ─────────────────────────────

def test_a_reason_quoting_agent_cli_words_is_stated_and_says_so(hook_env, monkeypatch):
    """Recording something afterwards is not forbidden — forbidding it would
    break a real need. The tier stays `stated`, because the row does point at
    recorded words; what changes is that the view now says which words."""
    dbp = hook_env
    monkeypatch.setattr("sys.stdin", _Stdin(lambda: False))
    assert cli._note(_note_args(text="finance asked for fiscal weeks", plan="P1", node="nk_a")) == 0

    conn = db.open_db(dbp)
    row = conn.execute("SELECT tier, utterance_origin FROM change_reason_v ORDER BY id").fetchall()
    conn.close()
    assert [tuple(r) for r in row] == [("stated", "agent_cli")]


def test_the_two_stated_rows_are_told_apart_by_origin_alone(hook_env):
    """The same sentence, the same tier, the same recorded_by: only origin
    separates the hook's capture from a record typed in afterwards."""
    dbp = hook_env
    conn = db.open_db(dbp)
    db.run_migrations(conn)
    text = "finance asked for fiscal weeks"
    captured = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P1", text=text,
                                   occurred_at="2026-09-15 10:00:00", origin="hook")
    typed_in = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P1", text=text,
                                   occurred_at="2026-09-15 10:00:00", origin="agent_cli")
    rows = {}
    for uid in (captured, typed_in):
        rid = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="organizational",
                              verbatim=(uid, 0, len(text)))
        r = pv.get_reason(conn, rid)
        rows[uid] = (r["tier"], r["recorded_by"], r["utterance_origin"])
    conn.close()
    assert rows[captured] == ("stated", "agent", "hook")
    assert rows[typed_in] == ("stated", "agent", "agent_cli")


# ── (f) a caller that says nothing claims nothing ──────────────────────────

def test_a_caller_that_names_no_origin_still_works_and_claims_nothing(conn):
    uid = pv.insert_utterance(conn, session_id="s", project="p", plan_id=None, text="words",
                              occurred_at="2026-09-15 10:00:00")
    assert pv.get_utterance(conn, uid)["origin"] == "unknown"
    assert pv.verify_chain(conn, "utterance")["ok"], "the default is hashed in like any other value"


def test_an_origin_outside_the_five_doors_is_refused_by_the_column(conn):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        pv.insert_utterance(conn, session_id="s", project="p", plan_id=None, text="words",
                            occurred_at="2026-09-15 10:00:00", origin="trust_me")
