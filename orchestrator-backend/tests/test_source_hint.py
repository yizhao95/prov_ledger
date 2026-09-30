"""A3: when the sentence already named the source, pin it while it is cheap.

A6 is the systematic pass at the end. This is the other half: the user has just
said "Sarah emailed that Q3 excludes EMEA", and at that moment the pointer costs a
single line of context to ask for. Later it costs a search.

The rule is deterministic — a word list, matched as text — so it needs no
calibration gate. Which email it actually is remains a judgement, and that
judgement stays with the host agent, which has the tools and the credentials. We
ask; it answers or it does not.

Two invariants the hook cannot break:

  it adds, it never vetoes   the utterance is recorded and the exit code is 0
                             whether the rule fires or not (NORTH-STAR)
  the harness is not a user  Claude Code delivers its own text through
                             UserPromptSubmit. That text is filtered out before
                             the rule is even consulted, so a task-notification
                             mentioning an email cannot make the hook prompt for a
                             pointer to words nobody said.

The word list lives in an importable module rather than inline in the hook, so it
can be read, asserted on and argued with.
"""
import io
import json
import sqlite3

import pytest

from orchestrator import hooks
from orchestrator.testing import source_words


def _hooks_source() -> str:
    from pathlib import Path
    return Path(hooks.__file__).read_text(encoding="utf-8")


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    errlog = tmp_path / "hook-errors.log"
    monkeypatch.setenv("ORCH_DB", str(dbp))
    monkeypatch.setenv("PROVLEDGER_HOOK_ERRORS", str(errlog))
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "proj", "repo": "/home/x/repo",
                                             "db_path": str(tmp_path / "g.db"), "commit_sha": "c"}]}))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    return dbp, errlog


def _submit(monkeypatch, capsys, prompt, cwd="/home/x/repo"):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "s", "cwd": cwd, "hook_event_name": "UserPromptSubmit", "prompt": prompt})))
    code = hooks.main(["UserPromptSubmit"])
    return code, capsys.readouterr().out


def _utterances(dbp) -> list:
    return sqlite3.connect(str(dbp)).execute("SELECT id, text, origin FROM utterance ORDER BY id").fetchall()


# ── (a) the hint names the command and the words it would hang on ────────────

def test_a_sentence_that_names_an_email_gets_one_hint_line(hook_env, monkeypatch, capsys):
    dbp, errlog = hook_env
    code, out = _submit(monkeypatch, capsys, "Sarah emailed that Q3 excludes EMEA")

    assert code == 0 and not errlog.exists()
    rows = _utterances(dbp)
    assert len(rows) == 1 and rows[0][2] == "hook"
    uid = rows[0][0]

    lines = out.strip().splitlines()
    assert len(lines) == 1, out
    assert "reference add" in lines[0], lines[0]
    assert f"--utterance {uid}" in lines[0], "the hint has to say which words the pointer would hang on"
    assert lines[0].startswith("provledger"), lines[0]


@pytest.mark.parametrize("prompt", [
    "Sarah emailed that Q3 excludes EMEA",
    "we agreed on the call that EMEA is out",
    "it is in the thread with finance",
    "in the meeting yesterday they said drop EMEA",
    "there is a ticket for this already",
    "JIRA-4417 says the rollup excludes EMEA",
    "she said in the review that paid orders only",
])
def test_the_english_half_of_the_list_fires(hook_env, monkeypatch, capsys, prompt):
    _, out = _submit(monkeypatch, capsys, prompt)
    assert "reference add" in out, f"{prompt!r} names a source and produced no hint"


# ── (b) the Chinese half fires too ───────────────────────────────────────────

@pytest.mark.parametrize("prompt", [
    "Sarah 邮件里说 Q3 不含 EMEA",
    "会上定了不含 EMEA",
    "群里发过这个结论",
    "工单里写了这条规则",
])
def test_the_chinese_half_of_the_list_fires(hook_env, monkeypatch, capsys, prompt):
    """The list carries both languages because the user writes in both, often in
    the same sentence. A rule that only reads English would go quiet exactly when
    it is most useful."""
    _, out = _submit(monkeypatch, capsys, prompt)
    assert "reference add" in out, f"{prompt!r} names a source and produced no hint"


# ── (c) ordinary work stays quiet ────────────────────────────────────────────

@pytest.mark.parametrize("prompt", [
    "refactor the rollup",
    "make load_orders keep only paid rows",
    "please keep fiscal weeks — do not switch them to calendar weeks",
    "run the tests again",
    "把汇总改成只算已付订单",
])
def test_a_sentence_that_names_no_source_produces_no_extra_line(hook_env, monkeypatch, capsys, prompt):
    dbp, errlog = hook_env
    code, out = _submit(monkeypatch, capsys, prompt)
    assert code == 0 and out == "", out
    assert len(_utterances(dbp)) == 1 and not errlog.exists()


# ── (d) it adds, it never vetoes ─────────────────────────────────────────────

@pytest.mark.parametrize("prompt", ["Sarah emailed that Q3 excludes EMEA", "refactor the rollup"])
def test_the_hook_exits_zero_and_records_the_words_either_way(hook_env, monkeypatch, capsys, prompt):
    dbp, errlog = hook_env
    code, _ = _submit(monkeypatch, capsys, prompt)
    assert code == 0
    assert [r[1] for r in _utterances(dbp)] == [prompt]
    assert not errlog.exists()


def test_the_hint_never_carries_a_permission_decision(hook_env, monkeypatch, capsys):
    """PreToolUse is the one hook allowed to deny anything. This one only talks."""
    _, out = _submit(monkeypatch, capsys, "Sarah emailed that Q3 excludes EMEA")
    for veto in ("permissionDecision", "deny", "block", "hookSpecificOutput"):
        assert veto not in out, out


def test_a_failing_database_still_exits_zero_and_says_nothing(hook_env, monkeypatch, capsys):
    """No utterance means no id to hang a pointer on, so there is nothing to ask
    for — and a hook that cannot write must not start printing instead."""
    _, errlog = hook_env
    monkeypatch.setattr("orchestrator.db.open_db",
                        lambda path: (_ for _ in ()).throw(sqlite3.OperationalError("unable to open database file")))
    code, out = _submit(monkeypatch, capsys, "Sarah emailed that Q3 excludes EMEA")
    assert code == 0 and out == ""
    assert len(errlog.read_text().splitlines()) == 1


def test_a_prompt_that_is_not_recorded_gets_no_hint(hook_env, monkeypatch, capsys):
    """A slash command names a source in its text sometimes; it is still not an
    utterance, and a hint pointing at an utterance that does not exist is a lie."""
    dbp, _ = hook_env
    code, out = _submit(monkeypatch, capsys, "/provledger-dashboard show me the email thread")
    assert code == 0 and out == ""
    assert _utterances(dbp) == []


# ── (e) the harness is not a user ────────────────────────────────────────────

@pytest.mark.parametrize("prefix", list(hooks.INJECTED_PROMPT_PREFIXES))
def test_injected_text_is_filtered_before_the_rule_is_consulted(hook_env, monkeypatch, capsys, prefix):
    """A finished subagent's report routinely contains the word "email". If the
    rule saw it, the hook would ask the user to pin a source for a sentence the
    user never said."""
    dbp, errlog = hook_env
    prompt = f"{prefix}\n<task-id>abc</task-id>\nthe subagent emailed the summary and read the thread\n"
    code, out = _submit(monkeypatch, capsys, prompt)
    assert code == 0 and out == "", out
    assert _utterances(dbp) == [] and not errlog.exists()


def test_the_filter_is_checked_inside_the_hint_rule_itself(hook_env):
    """Not only upstream. The rule is asked directly here, so the guarantee does
    not depend on one particular caller remembering to filter first."""
    for prefix in hooks.INJECTED_PROMPT_PREFIXES:
        assert hooks.source_hint(1, f"{prefix} Sarah emailed the numbers") is None


def test_the_rule_says_nothing_without_an_utterance_to_point_at(hook_env):
    assert hooks.source_hint(None, "Sarah emailed that Q3 excludes EMEA") is None
    assert hooks.source_hint(7, None) is None
    assert hooks.source_hint(7, "Sarah emailed that Q3 excludes EMEA") is not None


# ── (f) the word list is a module, not a literal in the hook ─────────────────

def test_the_word_list_is_importable_and_carries_both_languages():
    words = source_words.SOURCE_WORDS
    assert isinstance(words, tuple) and len(words) >= 10
    assert all(isinstance(w, str) and w and w == w.lower() for w in words), "the list is matched case-folded"
    assert len(set(words)) == len(words), "a duplicate trigger word is a trigger word nobody edited on purpose"
    assert any(w.isascii() for w in words), "the English half"
    assert any(not w.isascii() for w in words), "the Chinese half"
    for w in ("email", "邮件", "会上", "群里", "工单"):
        assert w in words, w


def test_the_matcher_returns_the_word_it_matched_and_nothing_otherwise():
    assert source_words.mentions_a_source("Sarah emailed that Q3 excludes EMEA") == "email"
    assert source_words.mentions_a_source("Sarah 邮件里说 Q3 不含 EMEA") == "邮件"
    assert source_words.mentions_a_source("refactor the rollup") is None
    assert source_words.mentions_a_source("") is None
    assert source_words.mentions_a_source(None) is None


def test_matching_ignores_case():
    assert source_words.mentions_a_source("SARAH EMAILED THE NUMBERS") == "email"
    assert source_words.mentions_a_source("JIRA-4417") == "jira"


def test_the_hook_does_not_carry_its_own_copy_of_the_list():
    """Two lists become two different lists. The hook reads this one."""
    src = _hooks_source()
    for w in ("in the thread", "on the call", "邮件", "会上", "群里", "工单"):
        assert w not in src, f"{w!r} is inlined in hooks.py instead of coming from the word list"
    assert "source_words" in src, "hooks.py has to read the list from somewhere"


# ── (g) string matching, and nothing else ────────────────────────────────────

def test_the_rule_calls_no_model_and_reaches_for_nothing():
    from pathlib import Path
    src = Path(source_words.__file__).read_text(encoding="utf-8")
    for forbidden in ("import subprocess", "import urllib", "requests", "httpx", "anthropic",
                      "claude", "runner", "prompt_model", "openai"):
        assert forbidden not in src, f"the word list must not {forbidden}"


def test_the_matcher_is_a_pure_function_of_its_argument():
    text = "Sarah emailed that Q3 excludes EMEA"
    got = [source_words.mentions_a_source(text) for _ in range(5)]
    assert got == ["email"] * 5


def test_the_hint_line_is_one_line_and_names_the_flags_a_caller_needs(hook_env):
    line = hooks.source_hint(42, "Sarah emailed that Q3 excludes EMEA")
    assert "\n" not in line.strip(), line
    for flag in ("reference add", "--utterance 42", "--kind", "--label", "--uri"):
        assert flag in line, flag
