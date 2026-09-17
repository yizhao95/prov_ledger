"""provledger.testing.claude_arbiter — OFFLINE (phase 8 Task 2, FL-040). The
runner is injected everywhere: no `claude` binary is ever executed here, and
the real arbiter runs only by hand (`analyzer arbiter-eval`), never in CI."""
import json
import os
import stat
import subprocess

import pytest

from orchestrator import graph_api as g
from orchestrator.testing import calibration as cal
from orchestrator.ask import runner as R
from orchestrator.testing import claude_arbiter as ca

ClaudeArbiter = lambda **kw: ca.ClaudeArbiter(**kw)   # resolved at call time (RED: the module is a stub)


def _row(qn, key="", struct="s", df="d", ctx=None, file="pkg/m.py"):
    return {"qualified_name": qn, "node_key": key, "node_type": "function", "file_path": file,
            "line_start": 3, "line_end": 5, "struct_sig": struct, "dataflow_sig": df, "context": ctx}


def _amb(prev=None, cur=None):
    return cal.ambiguity_from_item({"id": "e1", "ambiguity": {
        "layer": "struct_sig",
        "prev": prev or [_row("pkg.m.norm_a", "nk_a", ctx="    3  def norm_a(df):"), _row("pkg.m.norm_b", "nk_b", ctx="    7  def norm_b(df):")],
        "cur": cur or [_row("pkg.m.scale_x", ctx="    3  def scale_x(df):"), _row("pkg.m.scale_y", ctx="    7  def scale_y(df):")]}})


def _runner(answer):
    calls = []

    def run(prompt, *, model=None, timeout_s=None):
        calls.append({"prompt": prompt, "model": model, "timeout_s": timeout_s})
        return answer if isinstance(answer, str) else answer(prompt)
    run.calls = calls
    return run


def test_is_an_arbiter_and_loads_by_spec():
    a = ClaudeArbiter(runner=_runner(""))
    assert isinstance(a, g.Arbiter) and a.arbiter_id == "anthropic.claude_headless"
    assert isinstance(cal.load_arbiter("orchestrator.testing.claude_arbiter:ClaudeArbiter"), g.Arbiter)
    assert "Output ONLY one JSON object" in ca.prompt_text() and "pairs" in ca.prompt_text()


def test_valid_json_answer_becomes_assertions_with_evidence():
    run = _runner('{"pairs": [["pkg.m.norm_a", "pkg.m.scale_x"], ["pkg.m.norm_b", "pkg.m.scale_y"]], '
                  '"evidence": "main() calls scale_x then scale_y where it called norm_a then norm_b (context lines 12-13)"}')
    a = ClaudeArbiter(runner=run, model="claude-sonnet-5", timeout_s=7)
    out = a.arbitrate([_amb()])
    assert sorted((s.chosen_prev_key, s.cur_qualified_name) for s in out) == [("nk_a", "pkg.m.scale_x"), ("nk_b", "pkg.m.scale_y")]
    assert all(s.arbiter == "anthropic.claude_headless" and s.evidence.startswith("main() calls") for s in out)
    assert len(run.calls) == 1 and run.calls[0]["model"] == "claude-sonnet-5" and run.calls[0]["timeout_s"] == 7
    prompt = run.calls[0]["prompt"]
    assert prompt.startswith(ca.prompt_text().rstrip()) and "pkg.m.norm_a" in prompt and "pkg.m.scale_y" in prompt
    payload = json.loads(prompt.split("Material:\n", 1)[1])
    assert payload["layer"] == "struct_sig" and payload["signatures_equal"] == {"qualified_name": False, "struct_sig": True, "dataflow_sig": True}
    assert payload["previous"][0]["context"] == "    3  def norm_a(df):" and payload["current"][1]["lines"] == [3, 5]
    assert a.exchanges[-1]["verdict"] == "asserted"


@pytest.mark.parametrize("answer, verdict", [
    ('{"pairs": [["pkg.m.norm_a", "pkg.m.scale_x"]], "evidence": "   "}', "rejected: empty evidence"),
    ('{"pairs": [["pkg.m.norm_a", "pkg.m.other"]], "evidence": "x"}', "rejected: a name outside the ambiguity"),
    ('{"pairs": [["pkg.m.nope", "pkg.m.scale_x"]], "evidence": "x"}', "rejected: a name outside the ambiguity"),
    ('{"pairs": [["pkg.m.norm_a", "pkg.m.scale_x"], ["pkg.m.norm_a", "pkg.m.scale_y"]], "evidence": "x"}', "rejected: a name used twice"),
    ('I think norm_a became scale_x.', "rejected: not the JSON answer shape"),
    ('{"pairs": "norm_a->scale_x", "evidence": "x"}', "rejected: not the JSON answer shape"),
    ('', "rejected: not the JSON answer shape"),
    ('{"pairs": [], "evidence": "cannot tell: both bodies identical, no caller shown"}', "abstained"),
])
def test_bad_or_empty_answers_assert_nothing(answer, verdict):
    a = ClaudeArbiter(runner=_runner(answer))
    assert a.arbitrate([_amb()]) == [] and a.exchanges[-1]["verdict"] == verdict


def test_fenced_json_is_tolerated_and_partial_pairs_are_kept():
    a = ClaudeArbiter(runner=_runner('```json\n{"pairs": [["pkg.m.norm_a", "pkg.m.scale_x"]], "evidence": "only a is called"}\n```'))
    out = a.arbitrate([_amb()])
    assert [(s.chosen_prev_key, s.cur_qualified_name) for s in out] == [("nk_a", "pkg.m.scale_x")]


def test_context_falls_back_to_the_bound_repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("\n".join(f"line {i}" for i in range(1, 30)) + "\n")
    run = _runner('{"pairs": [], "evidence": "n/a"}')
    a = ClaudeArbiter(runner=run)
    a.bind_repo(str(tmp_path))
    a.arbitrate([_amb(prev=[_row("pkg.m.norm_a", "nk_a"), _row("pkg.m.norm_b", "nk_b")], cur=[_row("pkg.m.scale_x"), _row("pkg.m.scale_y")])])
    payload = json.loads(run.calls[0]["prompt"].split("Material:\n", 1)[1])
    ctx = payload["previous"][0]["context"]
    assert ctx.startswith("    1  line 1") and "   15  line 15" in ctx and "   16  line 16" not in ctx


def test_default_runner_runs_headless_claude_with_the_prompt_on_stdin(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text('#!/usr/bin/env bash\nprompt=$(cat)\nprintf \'{"type":"result","result":"{\\\\"pairs\\\\": [], \\\\"evidence\\\\": \\\\"seen: %s args: %s\\\\"}"}\' "$(echo "$prompt" | head -c 20 | tr -d \'\\n\')" "$*"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    text, detail = ca.default_runner("You are an identity arbiter…", model="claude-haiku-4-5", timeout_s=10)
    parsed = ca.parse_answer(text)
    assert parsed is not None and parsed[0] == []
    assert "seen: You are an identity" in parsed[1]
    assert "-p --output-format json --max-turns 1 --tools  --no-session-persistence --model claude-haiku-4-5" in parsed[1]
    assert detail["rc"] == 0 and isinstance(detail["elapsed_ms"], int) and detail["model"] == "claude-haiku-4-5"


def test_default_runner_says_why_it_came_back_empty(tmp_path, monkeypatch):
    """It used to return '' for a timeout, a crash and garbage alike, and the
    caller printed "no model" for all three. The reason travels now."""
    fake = tmp_path / "claude"
    fake.chmod(0o755) if fake.exists() else fake.write_text("#!/usr/bin/env bash\nexit 3\n")
    fake.write_text("#!/usr/bin/env bash\necho 'boom: bad credentials' >&2\nexit 3\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    text, detail = ca.default_runner("x", timeout_s=5)
    assert text == "" and detail["rc"] == 3 and "boom: bad credentials" in detail["stderr_head"]
    assert detail["reason"] == "non-zero exit"
    assert ca.text_runner("x", timeout_s=5) == "", "the arbiter still sees a plain empty string"

    fake.write_text("#!/usr/bin/env bash\necho not json\n")
    text, detail = ca.default_runner("x", timeout_s=5)
    assert text == "" and detail["rc"] == 0 and detail["reason"] == "output was not JSON"

    fake.write_text("#!/usr/bin/env bash\nsleep 5\n")
    with pytest.raises(R.RunnerTimeout) as e:
        ca.default_runner("x", timeout_s=0.3)
    assert e.value.detail["timeout_s"] == 0.3
    assert ca.text_runner("x", timeout_s=0.3) == ""

    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    with pytest.raises(R.RunnerError):
        ca.default_runner("x", timeout_s=5)
    assert ca.text_runner("x", timeout_s=5) == ""


# Real, on this machine: the account's Fable budget ran out. `claude -p` exited
# 1 and printed exactly this — good JSON, `is_error`, and the only sentence in
# the whole run worth reading. It used to be discarded as "non-zero exit".
FABLE_LIMIT_JSON = '{"type": "result", "subtype": "error_during_execution", "is_error": true, "duration_ms": 1183, "num_turns": 0, "result": "You\'ve reached your Fable limit. Switch to another model, or manage usage credits at https://claude.ai/settings/usage", "session_id": "5f0f2c3a-0000-4000-8000-000000000000"}'


def test_default_runner_keeps_the_sentence_the_model_refused_with(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text("#!/usr/bin/env bash\ncat > /dev/null\ncat <<'JSONEOF'\n" + FABLE_LIMIT_JSON + "\nJSONEOF\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    text, detail = ca.default_runner("x", timeout_s=10)
    assert text == ""
    assert detail["refused"] is True and detail["is_error"] is True and detail["rc"] == 1
    assert detail["result"].startswith("You've reached your Fable limit")
    assert R.call(ca.default_runner, "x", timeout_s=10)[2] == "refused"
    assert ca.text_runner("x", timeout_s=10) == "", "the arbiter still sees a plain empty string"


def test_an_is_error_answer_is_a_refusal_even_on_a_zero_exit(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text("#!/usr/bin/env bash\ncat > /dev/null\ncat <<'JSONEOF'\n" + FABLE_LIMIT_JSON + "\nJSONEOF\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    text, detail = ca.default_runner("x", timeout_s=10)
    assert text == "" and detail["refused"] is True and detail["rc"] == 0


def test_the_model_from_the_environment_reaches_the_command_line(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text('#!/usr/bin/env bash\ncat > /dev/null\nprintf \'{"type":"result","result":"args: %s"}\' "$*"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PROVLEDGER_ASK_MODEL", "sonnet")

    text, detail, outcome = R.call(ca.default_runner, "x", timeout_s=10)
    assert outcome == "ok" and "--model sonnet" in text
    assert detail["model"] == "sonnet"

    text, detail, _ = R.call(ca.default_runner, "x", model="haiku", timeout_s=10)
    assert "--model haiku" in text and detail["model"] == "haiku"


def _stub_by_suffix(prompt):
    payload = json.loads(prompt.split("Material:\n", 1)[1])
    by_suffix = {r["qualified_name"].rsplit("_", 1)[-1]: r["qualified_name"] for r in payload["previous"]}
    pairs = [[by_suffix[c["qualified_name"].rsplit("_", 1)[-1]], c["qualified_name"]]
             for c in payload["current"] if c["qualified_name"].rsplit("_", 1)[-1] in by_suffix]
    return json.dumps({"pairs": pairs, "evidence": "stub: shared name suffix"})


def test_calibration_run_with_a_stub_runner_is_consistent_and_clears_the_gate(tmp_path):
    items = []
    for i in range(10):
        a, b = f"pkg.m.h{i}_a", f"pkg.m.h{i}_b"
        items.append({"id": f"e{i}", "ambiguity": {"layer": "struct_sig", "prev": [_row(a, f"ka{i}"), _row(b, f"kb{i}")],
                                                   "cur": [_row(f"pkg.m.g{i}_a"), _row(f"pkg.m.g{i}_b")]},
                      "truth": {"pairs": [[a, f"pkg.m.g{i}_a"], [b, f"pkg.m.g{i}_b"]]}, "labelled_by": "test"})
    calib = tmp_path / "calib.json"
    calib.write_text(json.dumps({"version": 1, "items": items}))
    rep = cal.run(ClaudeArbiter(runner=_runner(_stub_by_suffix)), calib, n_runs=3, out_dir=tmp_path / "eval")
    assert (rep.consistency, rep.coverage, rep.accuracy, rep.evidence_ok, rep.n_truth) == (1.0, 1.0, 1.0, True, 10)
    assert rep.arbiter_id == "anthropic.claude_headless" and cal.gate(rep.arbiter_id, calib, tmp_path / "eval")[0] is True
