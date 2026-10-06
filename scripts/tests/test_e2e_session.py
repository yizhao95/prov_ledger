"""The release check's real-session plumbing (scripts/release_e2e/plugin_session.py).

Stage 0 and stage 2 drive real headless `claude` sessions with the plugin
installed, and judge them by what the session actually did: which plugin was
loaded, which commands ran, what each one printed, what the session finally
said. All of that comes out of `claude -p --output-format stream-json
--verbose`, one JSON event per line. The fixture below is a trimmed copy of a
real stream: a successful call, a failed one (`is_error`), and the result.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release_e2e"))


@pytest.fixture
def PS():
    import plugin_session
    return plugin_session

INIT = {"type": "system", "subtype": "init", "session_id": "s-1", "cwd": "/w/proj",
        "plugins": [{"name": "provledger", "path": "/c/plugins/cache/provledger/provledger/0.4.4",
                     "source": "provledger@provledger", "version": "0.4.4"}],
        "slash_commands": ["provledger:ledger", "provledger:receipts"], "skills": ["provledger:ledger"]}


def _use(tid, command):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}]}}


def _result(tid, content, is_error=False):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": content}]}}


STREAM = [
    {"type": "system", "subtype": "hook_started"},
    INIT,
    _use("t1", 'provledger ask "why 70/30?" --json --no-model'),
    _result("t1", '{"ask_id": 4, "facts_text": "#2 the split is 70/30"}'),
    {"type": "rate_limit_event"},
    _use("t2", "command -v definitely-not-a-tool"),
    _result("t2", "Exit code 1", is_error=True),
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking aloud"}]}},
    _use("t3", "provledger ask submit 4 --answer-file /tmp/a.md"),
    _result("t3", [{"type": "text", "text": "1 sentence kept, 0 dropped"}]),
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 4,
     "result": "The split is 70/30 [#2].", "session_id": "s-1", "permission_denials": []},
]


def _lines(events):
    return [json.dumps(e) for e in events]


def test_the_session_and_its_plugins_come_from_the_init_event(PS):
    t = PS.parse_stream(_lines(STREAM))
    assert t.session_id == "s-1"
    assert t.plugins == {"provledger": "/c/plugins/cache/provledger/provledger/0.4.4"}
    assert "provledger:ledger" in t.slash_commands


def test_each_call_is_paired_with_its_own_result(PS):
    t = PS.parse_stream(_lines(STREAM))
    assert [(c.name, c.input["command"].split()[0], c.is_error) for c in t.calls] == [
        ("Bash", "provledger", False), ("Bash", "command", True), ("Bash", "provledger", False)]
    assert t.calls[1].output == "Exit code 1"
    assert t.calls[2].output == "1 sentence kept, 0 dropped", "a block list is flattened to its text"


def test_the_final_answer_and_turns_come_from_the_result_event(PS):
    t = PS.parse_stream(_lines(STREAM))
    assert (t.ok, t.result, t.num_turns) == (True, "The split is 70/30 [#2].", 4)


def test_bash_commands_in_the_order_they_ran(PS):
    assert PS.parse_stream(_lines(STREAM)).bash_commands() == [
        'provledger ask "why 70/30?" --json --no-model',
        "command -v definitely-not-a-tool",
        "provledger ask submit 4 --answer-file /tmp/a.md"]


def test_the_material_is_what_the_provledger_calls_printed_and_nothing_else(PS):
    material = PS.parse_stream(_lines(STREAM)).provledger_material()
    assert '"facts_text": "#2 the split is 70/30"' in material
    assert "1 sentence kept" in material
    assert "Exit code 1" not in material


def test_a_line_that_is_not_json_is_skipped(PS):
    t = PS.parse_stream(["not json {", *_lines(STREAM)])
    assert t.result == "The split is 70/30 [#2]."


def test_a_stream_without_a_result_event_is_not_ok(PS):
    t = PS.parse_stream(_lines(STREAM[:4]))
    assert (t.ok, t.result) == (False, "")
    assert len(t.calls) == 1


def test_an_error_result_is_not_ok(PS):
    bad = {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 30, "result": ""}
    assert PS.parse_stream(_lines([INIT, bad])).ok is False


def test_seed_config_carries_the_login_and_nothing_of_the_hosts_plugins(PS, tmp_path):
    real = tmp_path / "real"
    (real / ".claude").mkdir(parents=True)
    (real / ".claude" / ".credentials.json").write_text('{"token": "t"}')
    (real / ".claude" / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "a@b"}, "userID": "u", "projects": {"/x": {}}, "theme": "dark"}))
    cfg = tmp_path / "cfg"
    PS.seed_config(cfg, real)
    assert json.loads((cfg / ".credentials.json").read_text()) == {"token": "t"}
    account = json.loads((cfg / ".claude.json").read_text())
    assert account["oauthAccount"] == {"emailAddress": "a@b"} and account["hasCompletedOnboarding"] is True
    assert "projects" not in account and "theme" not in account
    assert json.loads((cfg / "settings.json").read_text()) == {"language": "en"}, \
        "no enabledPlugins key: the plugin under test is installed into this config next"


def test_seed_config_falls_back_to_the_home_level_account_file(PS, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / ".claude.json").write_text(json.dumps({"userID": "u"}))
    cfg = tmp_path / "cfg"
    PS.seed_config(cfg, real)
    assert json.loads((cfg / ".claude.json").read_text())["userID"] == "u"


def test_a_stranger_has_none_of_the_developers_paths(PS, tmp_path):
    venv_bin = tmp_path / "dev-venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "provledger").write_text("#!/bin/sh\n")
    base = {"PATH": f"{venv_bin}:/usr/bin:/bin", "ORCH_DB": "/real/orchestrator.db",
            "PSG_REGISTRY_PATH": "/real/projects.json", "PROVLEDGER_VENV": "/real/.venv",
            "PROVLEDGER_HEADLESS": "1", "CLAUDE_PLUGIN_ROOT": "/real/plugin", "LANG": "C.UTF-8"}
    env = PS.stranger_env(tmp_path / "home", tmp_path / "cfg", base)
    assert env["HOME"] == str(tmp_path / "home") and env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    for k in ("ORCH_DB", "PSG_REGISTRY_PATH", "PROVLEDGER_VENV", "PROVLEDGER_HEADLESS", "CLAUDE_PLUGIN_ROOT"):
        assert k not in env, k
    assert env["PATH"] == "/usr/bin:/bin", "a directory that already holds provledger is not a stranger's"
    assert env["LANG"] == "C.UTF-8"


@pytest.fixture
def S2():
    import stage2_surfaces
    return stage2_surfaces


QUESTION = {"id": "Q1-cause", "surface": "ask", "question": "why 70/30?", "must": ["70/30"]}


def test_a_real_answer_carries_what_its_own_session_read_and_ran(PS, S2):
    t = PS.parse_stream(_lines(STREAM))
    a = S2.real_answer(QUESTION, t)
    assert a["id"] == "Q1-cause" and a["must"] == ["70/30"]
    assert a["answer"] == "The split is 70/30 [#2]."
    assert '"facts_text": "#2 the split is 70/30"' in a["material"]
    assert a["commands"][0].startswith("provledger ask ")
    assert a["outcome"] == "ok" and a["session_id"] == "s-1"


def test_a_session_that_never_finished_has_no_answer(PS, S2):
    a = S2.real_answer(QUESTION, PS.parse_stream(_lines(STREAM[:4])))
    assert a["answer"] == "" and a["outcome"] != "ok"


def test_stage_3_marks_the_real_answers_and_keeps_the_simulated_ones_apart(S2):
    real = [{**QUESTION, "answer": "real 70/30 [#2]", "outcome": "ok"},
            {**QUESTION, "id": "Q2-when", "answer": "", "outcome": "failed"}]
    simulated = [{**QUESTION, "answer": "simulated", "outcome": "ok"}]
    doc = S2.stage3_payload({"project": "p"}, real, simulated)
    assert [a["id"] for a in doc["answers"]] == ["Q1-cause"], "a question with no real answer is not marked"
    assert doc["answers"][0]["answer"] == "real 70/30 [#2]"
    assert doc["simulated"] == simulated and doc["facts"] == {"project": "p"}


COMPOUND = [
    INIT,
    _use("c1", "grep -rn discount . ; ls; provledger --help 2>&1 | head -30"),
    _result("c1", "pkg/rollup.py:7: given = sum(o['discount'] ...)\nusage: provledger"),
    _use("c2", 'P=demo; provledger receipts candidates "who; and why" --project $P'),
    _result("c2", "candidates: pkg.rollup.discount_rate"),
    _use("c3", "provledger plan p-1; echo =====; provledger receipts facts a b --project demo"),
    _result("c3", "plan p-1 ...\n===\nfacts ..."),
    _use("c4", "for r in 1 2; do provledger record \"#$r\"; done"),
    _result("c4", "Permission denied", is_error=True),
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "c5", "name": "Read", "input": {"file_path": "/w/pkg/rollup.py"}}]}},
    _result("c5", "22  return {'discount_rate_is_estimated': True}"),
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 6, "result": "reply"},
]


def test_every_provledger_subcommand_is_found_inside_compound_and_prefixed_commands(PS):
    """An agent writes `P=x; provledger receipts candidates …` and `a; echo; provledger …`:
    a check that only looks at how a command starts misses reads that did run."""
    inv = PS.parse_stream(_lines(COMPOUND)).invocations()
    assert inv[0].startswith('receipts candidates "who; and why"'), "a quoted ; does not split"
    assert [i.split()[0:2] for i in inv] == [["receipts", "candidates"], ["plan", "p-1"], ["receipts", "facts"]]


def test_a_flag_is_not_a_read_and_a_failed_call_read_nothing(PS):
    inv = PS.parse_stream(_lines(COMPOUND)).invocations()
    assert not any(i.startswith("--help") for i in inv)
    assert not any(i.startswith("record") for i in inv), "the denied loop never ran"


def test_the_code_a_session_read_is_kept_apart_from_its_ledger_reads(PS):
    t = PS.parse_stream(_lines(COMPOUND))
    code = t.code_material()
    assert "discount_rate_is_estimated" in code and "pkg/rollup.py:7" in code
    assert "candidates: pkg.rollup.discount_rate" not in code
    ledger = t.provledger_material()
    assert "candidates: pkg.rollup.discount_rate" in ledger and "facts ..." in ledger
    assert "discount_rate_is_estimated" not in ledger


def test_a_real_answer_carries_the_code_its_session_read(PS, S2):
    a = S2.real_answer(QUESTION, PS.parse_stream(_lines(COMPOUND)))
    assert "discount_rate_is_estimated" in a["code"]
    assert "discount_rate_is_estimated" not in a["material"]
