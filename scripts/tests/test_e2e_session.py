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


@pytest.fixture
def S3():
    import stage3_judge
    return stage3_judge


def _card(points, unsupported=False, missed=False):
    return {"points": [{"point": p, "verdict": v} for p, v in points],
            "unsupported_claim": {"found": unsupported}, "missed_fact": {"found": missed}}


def test_a_judge_that_agrees_passes(S3):
    from e2elib import OK
    assert S3.judge_verdict(_card([("a", "hit"), ("b", "hit")])) == OK
    assert S3.judge_verdict(_card([("a", "hit"), ("b", "partial")], missed=True)) == OK


def test_a_judge_that_disagrees_is_a_finding_for_a_person_never_a_fail(S3):
    """The real sessions are nondeterministic and the judge is a model: across two
    runs different questions fell. The user's rule: a disagreement goes to a
    person before release; it does not decide the release on its own."""
    from e2elib import FINDING
    assert S3.judge_verdict(_card([("a", "hit")], unsupported=True)) == FINDING
    assert S3.judge_verdict(_card([("a", "miss"), ("b", "hit")])) == FINDING


def test_refresh_login_copies_the_current_credential_over_a_stale_copy(PS, tmp_path):
    """A release check runs for over an hour on a copied OAuth credential. When the
    developer's own session rotated the refresh token, the copy went stale and every
    later model call failed ("OAuth session expired and could not be refreshed").
    The login is copied again right before each call."""
    real = tmp_path / "real"
    (real / ".claude").mkdir(parents=True)
    (real / ".claude" / ".credentials.json").write_text('{"token": "old"}')
    cfg = tmp_path / "cfg"
    PS.seed_config(cfg, real)
    (real / ".claude" / ".credentials.json").write_text('{"token": "rotated"}')
    PS.refresh_login(cfg, real)
    assert json.loads((cfg / ".credentials.json").read_text()) == {"token": "rotated"}


def test_every_session_refreshes_its_login_first(PS, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(PS, "refresh_login", lambda cfg, real: seen.append(str(cfg)))
    monkeypatch.setattr(PS.subprocess, "run", lambda *a, **k: PS.subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    PS.run_session("hi", cwd=tmp_path, env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")})
    assert seen == [str(tmp_path / "cfg")]


def test_the_judges_calls_refresh_the_login_too(tmp_path, monkeypatch):
    import session_model as SM
    import plugin_session as PS
    seen = []
    monkeypatch.setattr(PS, "refresh_login", lambda cfg, real: seen.append(str(cfg)))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "judge-cfg"))
    monkeypatch.setattr(SM.subprocess, "run", lambda *a, **k: SM.subprocess.CompletedProcess(a, 0, stdout='{"result": "ok"}', stderr=""))
    SM.call("prompt")
    assert seen == [str(tmp_path / "judge-cfg")]


def _function(source: str, name: str) -> str:
    start = source.index(f"def {name}(")
    end = source.find("\ndef ", start + 1)
    return source[start:end if end > 0 else None]


def test_the_dummy_load_orders_reads_the_discount_and_then_stops():
    """Plan A's analysis says load_orders is one of the three readers of
    orders.discount, and Q4 / Q7 ask why it changed and who signed it off. In
    v1 it never read the column and v2 changed only its docstring, so its
    structure never changed: it looked "changed and unexplained" only because the
    review built a fresh graph with no history (FL-084). With that fixed it read
    as untouched and the release check had no unexplained node to ask about."""
    import dummy_project as DP
    v1 = _function(DP.V1["pkg/rollup.py"], "load_orders")
    v2 = _function(DP.V2["pkg/rollup.py"], "load_orders")
    assert "discount" in v1.split('"""')[-1], "v1 load_orders reads orders.discount in its code"
    assert "discount" not in v2.split('"""')[-1], "v2 load_orders no longer reads it"


def _login(real, expires_in_s):
    import time
    (real / ".claude").mkdir(parents=True, exist_ok=True)
    oauth = {"accessToken": "a", "refreshToken": "r"}
    if expires_in_s is not None:
        oauth["expiresAt"] = int((time.time() + expires_in_s) * 1000)
    (real / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))


def test_a_call_the_login_would_expire_during_is_not_made(PS, tmp_path):
    """A sandbox session whose access token expires refreshes it inside the sandbox,
    and the developer's own refresh token then stops working. A call that may run
    past the expiry (its timeout plus a margin) is not made; the verdict says how
    long the login had left."""
    _login(tmp_path / "real", 8 * 60)
    ready, why = PS.login_ready(tmp_path / "cfg", tmp_path / "real", run_for_s=240)
    assert ready is False
    assert "expires in 8 min" in why and "not made" in why


def test_a_call_with_time_to_spare_goes_ahead(PS, tmp_path):
    _login(tmp_path / "real", 2 * 3600)
    assert PS.login_ready(tmp_path / "cfg", tmp_path / "real", run_for_s=1200)[0] is True
    assert (tmp_path / "cfg" / ".credentials.json").is_file(), "the current login was copied in"


def test_a_login_without_an_expiry_does_not_block(PS, tmp_path):
    _login(tmp_path / "real", None)
    assert PS.login_ready(tmp_path / "cfg", tmp_path / "real", run_for_s=240)[0] is True


def test_a_blocked_session_is_never_started_and_reads_as_blocked(PS, tmp_path, monkeypatch):
    import stage2_surfaces as S2
    monkeypatch.setattr(PS, "login_ready", lambda cfg, real, run_for_s: (False, "the login expires in 3 min"))

    def never(*a, **k):
        raise AssertionError("a session was started")

    monkeypatch.setattr(PS.subprocess, "run", never)
    s = PS.run_session("hi", cwd=tmp_path, env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")})
    assert s.blocked == "the login expires in 3 min" and not s.ok
    assert S2.real_answer({"id": "Q1"}, s)["outcome"] == "blocked"
    assert PS.session_verdict(s) == S2.BLOCKED


def test_a_finished_or_failed_session_keeps_its_verdict(PS):
    from e2elib import FAIL, OK
    assert PS.session_verdict(PS.Transcript(ok=True)) == OK
    assert PS.session_verdict(PS.Transcript(ok=False, rc=1)) == FAIL


def test_a_blocked_model_call_is_never_made_and_the_model_reads_as_unreachable(tmp_path, monkeypatch):
    import plugin_session as PS
    import session_model as SM
    monkeypatch.setattr(PS, "login_ready", lambda cfg, real, run_for_s: (False, "the login expires in 3 min"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "judge-cfg"))

    def never(*a, **k):
        raise AssertionError("a model call was made")

    monkeypatch.setattr(SM.subprocess, "run", never)
    text, detail, outcome = SM.call("prompt")
    assert (text, outcome) == ("", "blocked") and detail["reason"] == "the login expires in 3 min"
    assert SM.available() == (False, "blocked: the login expires in 3 min")


def test_the_build_can_wait_for_the_ledger_clock_to_pass_a_stamp(tmp_path, monkeypatch):
    """Plan B's measurement has to come strictly after plan A's expectation, and
    db.get_metrics compares the two on the ledger clock, which has one-second
    resolution. Once plan A's close got fast, both landed in the same second: the
    measurement counted as `before`, backfill wrote `none_available`, and plan A's
    claim was never contradicted (kind 3/4)."""
    import sqlite3
    import dummy_project as DP
    db = tmp_path / "o.db"
    sqlite3.connect(str(db)).close()
    monkeypatch.setenv("ORCH_DB", str(db))
    b = object.__new__(DP.Build)                    # rows() reads ORCH_DB and nothing else
    now = b.rows("SELECT CURRENT_TIMESTAMP")[0][0]
    b.wait_past(now)
    assert b.rows("SELECT CURRENT_TIMESTAMP")[0][0] > now
