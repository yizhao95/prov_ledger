"""ask — the model path, and what it says when the model does not answer.

`summary unavailable: no model` used to be printed for four different things:
no runner at all, no candidate node, a runner that came back empty, and a
runner that raised or timed out. The worst of them was the third: with the
backend installed under its wheel name (`provledger`), `summarize.prompt_text`
asked for `orchestrator.testing` and raised ModuleNotFoundError INSIDE the
`try` that wrapped the model call — so a packaging bug read, to the person at
the terminal, as "you have no model". A tool that hides the reason it failed
is the thing this project exists to prevent.

So: one note per cause, the cause in `degraded_reason`, and the raw numbers
(rc, the head of stderr, the wall time, the length of the answer) appended to
`ask_log.runner_detail`.
"""
import json
import sys
from pathlib import Path

import pytest

from orchestrator import ask, cli, provenance as pv
from orchestrator.ask import facts as F, locate, runner as R, summarize as SU

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

QUESTION = "why did we stop using orders.discount? was it ever tested?"
NOWHERE = "what did the zzzqqq widget decide about xylophones?"


# ── the demo, in miniature: three nodes the question literally touches ────────

@pytest.fixture
def graph(tmp_path):
    path = tmp_path / "demo-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    for key, qn in (("nk_lo", "pkg.rollup.load_orders"), ("nk_dr", "pkg.rollup.discount_rate"),
                    ("nk_dt", "pkg.tiles.dashboard_tile")):
        ps.add_snapshot(c, 1, key, qn)
        ps.add_event(c, 1, 1, "node_added", key, created_at="2026-09-17T09:00:00+00:00")
    c.commit(); c.close()
    return str(path)


@pytest.fixture
def seeded(conn, graph):
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0",
                            text="Drop orders.discount from the rollup — upstream said the v2 feed no longer carries it",
                            occurred_at="2026-09-17 10:00:00")
    ref = pv.insert_reference(conn, project="proj", kind="email", label="Re: orders feed v2 schema (demo)",
                              occurred_at="2026-09-17 09:00:00", uri="mailto:data-platform@example.com")
    rid = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_dr", kind="technical", role="reason",
                           verbatim=(u, 0, 40), recorded_by="human", occurred_at="2026-09-17 10:00:00")
    pv.link_reference(conn, rid, ref)
    ids = {"reason": rid}
    for n, key in (("c1", "nk_lo"), ("c2", "nk_dt")):
        cid = pv.insert_reason(conn, project="proj", plan_id="P0", node_key=key, kind="technical", role="constraint",
                               statement="Do not depend on orders.discount: the v2 upstream feed no longer provides it",
                               recorded_by="human", occurred_at="2026-09-17 10:05:00")
        pv.link_reference(conn, cid, ref)
        ids[n] = cid
    conn.commit()
    return ids


@pytest.fixture
def ft(conn, graph, seeded):
    return F.facts(conn, graph, ["pkg.rollup.discount_rate"], project="proj")


def _runner(answer):
    def run(prompt, *, model=None, timeout_s=None):
        return answer
    return run


# ── the regression that started it: the candidates must reach the model ──────

def test_the_candidates_the_code_found_are_in_the_prompt_the_model_sees(conn, graph, seeded):
    """3 candidates with no model must be the same 3 in the choose prompt."""
    prompts = []

    def spy(prompt, *, model=None, timeout_s=None):
        prompts.append(prompt)
        return '{"chosen": ["pkg.rollup.discount_rate"], "basis": "the question names the column"}'

    cands = locate.candidates(conn, graph, QUESTION, project="proj")
    assert [c["qn"] for c in cands] and len(cands) == 3
    doc = ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph, runner=spy, record=False)
    assert len(doc["candidates"]) == 3
    assert prompts, "the model was never asked to choose"
    for qn in ("pkg.rollup.load_orders", "pkg.rollup.discount_rate", "pkg.tiles.dashboard_tile"):
        assert qn in prompts[0], f"{qn} is a candidate the model never saw"
    assert "(none)" not in prompts[0]
    assert doc["chosen"]["chosen"] == ["pkg.rollup.discount_rate"] and doc["chosen"]["fallback"] is False


def test_both_steps_call_the_model_when_there_are_candidates(conn, graph, seeded):
    calls = []

    def spy(prompt, *, model=None, timeout_s=None):
        calls.append(prompt)
        return ('{"chosen": ["pkg.rollup.discount_rate"], "basis": "named"}' if not calls[1:]
                else f"The column was dropped [#{1}].")

    ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph, runner=spy, record=False)
    assert len(calls) == 2, "choose ran but summarize never reached the model"


# ── one note per cause ───────────────────────────────────────────────────────

def test_no_runner_says_no_model_configured(conn, graph, seeded):
    doc = ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph, runner=None, record=False)
    assert doc["degraded"] is True and doc["degraded_reason"] == "no_model"
    assert doc["note"] == SU.NO_MODEL_NOTE == "summary unavailable: no model configured"


def test_no_candidate_node_says_so_and_never_calls_the_model(conn, graph, seeded):
    calls = []
    doc = ask.run(conn, project="proj", question=NOWHERE, psg_db_path=graph, record=False,
                  runner=lambda p, *, model=None, timeout_s=None: calls.append(p) or "")
    assert doc["candidates"] == [] and calls == []
    assert doc["degraded_reason"] == "no_candidates"
    assert doc["note"] == SU.NO_CANDIDATES_NOTE == "summary unavailable: no candidate nodes matched the question"


def test_an_empty_answer_reports_the_return_code_and_the_head_of_stderr(conn, graph, seeded):
    def empty(prompt, *, model=None, timeout_s=None):
        return "", {"rc": 3, "stderr_head": "Invalid API key · Please run /login"}

    doc = ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph, runner=empty, record=False)
    assert doc["degraded_reason"] == "empty"
    assert doc["note"].startswith("summary unavailable: model returned nothing")
    assert "rc 3" in doc["note"] and "Invalid API key" in doc["note"]


def test_a_runner_that_raises_says_which_exception(conn, graph, seeded):
    def boom(prompt, *, model=None, timeout_s=None):
        raise RuntimeError("claude is not on PATH")

    doc = ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph, runner=boom, record=False)
    assert doc["degraded_reason"] == "failed"
    assert doc["note"] == "summary unavailable: model call failed: RuntimeError: claude is not on PATH"


def test_a_runner_that_times_out_says_the_budget_it_spent(conn, graph, seeded):
    def slow(prompt, *, model=None, timeout_s=None):
        raise R.RunnerTimeout("timed out", {"timeout_s": 7})

    doc = ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph, runner=slow, record=False)
    assert doc["degraded_reason"] == "timeout"
    assert doc["note"] == "summary unavailable: model call timed out after 7 s"


def test_a_prompt_that_cannot_be_built_is_not_reported_as_no_model(conn, ft, monkeypatch):
    """FL-067 again: the wheel installs the backend as `provledger`, so the old
    hardcoded `orchestrator.testing` raised inside the try around the call."""
    def missing():
        raise ModuleNotFoundError("No module named 'orchestrator'")

    monkeypatch.setattr(SU, "prompt_text", missing)
    got = SU.summarize(QUESTION, ft, runner=_runner("anything"), candidates=3)
    assert got["degraded_reason"] == "failed"
    assert "No module named 'orchestrator'" in got["note"]
    assert got["note"] != SU.NO_MODEL_NOTE


def test_the_prompt_is_found_under_whatever_name_the_package_is_installed_as():
    src = Path(SU.__file__).read_text(encoding="utf-8")
    assert '"orchestrator' not in src and "'orchestrator" not in src, "the package name is not a literal"
    assert SU.TESTING_PACKAGE == SU.__package__.rsplit(".", 1)[0] + ".testing"
    assert "{question}" in SU.prompt_text() and "{facts}" in SU.prompt_text()


# ── the numbers go into ask_log, append-only ─────────────────────────────────

@pytest.mark.parametrize("make,reason", [
    (lambda: (lambda p, *, model=None, timeout_s=None: ("", {"rc": 3, "stderr_head": "boom"})), "empty"),
    (lambda: (lambda p, *, model=None, timeout_s=None: (_ for _ in ()).throw(RuntimeError("nope"))), "failed"),
])
def test_ask_log_keeps_the_runner_detail_of_a_failed_summary(conn, graph, seeded, make, reason):
    doc = ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph, runner=make(), runner_name="stub")
    row = ask.get_ask(conn, doc["ask_id"])
    detail = row["runner_detail"]
    assert detail and detail["summarize"]["outcome"] == reason
    assert detail["summarize"]["note"] == doc["note"]
    assert isinstance(detail["summarize"]["detail"].get("elapsed_ms"), int)
    assert detail["choose"]["outcome"] in ("ok", "empty", "failed", "timeout")


def test_ask_log_keeps_the_raw_and_the_wall_time_of_a_good_summary(conn, graph, seeded):
    def spy(prompt, *, model=None, timeout_s=None):
        if "picking which nodes" in prompt:
            return '{"chosen": ["pkg.rollup.discount_rate"], "basis": "named"}'
        return f"The column was dropped [#{1}]."

    doc = ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph, runner=spy, runner_name="stub")
    row = ask.get_ask(conn, doc["ask_id"])
    assert row["runner_detail"]["summarize"]["outcome"] == "ok"
    assert row["runner_detail"]["summarize"]["raw"].startswith("The column was dropped")
    assert row["runner_detail"]["summarize"]["detail"]["raw_len"] > 0


# ── the CLI: the env var and the budget ──────────────────────────────────────

def test_ask_runner_defaults_to_PROVLEDGER_ASK_RUNNER(monkeypatch):
    monkeypatch.delenv("PROVLEDGER_ASK_RUNNER", raising=False)
    assert cli.build_parser().parse_args(["ask", "q"]).runner == "claude"
    monkeypatch.setenv("PROVLEDGER_ASK_RUNNER", "stub")
    assert cli.build_parser().parse_args(["ask", "q"]).runner == "stub"
    assert cli.build_parser().parse_args(["ask", "q", "--runner", "claude"]).runner == "claude"
    monkeypatch.setenv("PROVLEDGER_ASK_RUNNER", "NONE")
    assert cli.build_parser().parse_args(["ask", "q"]).runner == "none"
    monkeypatch.setenv("PROVLEDGER_ASK_RUNNER", "gpt-9")
    assert cli.build_parser().parse_args(["ask", "q"]).runner == "claude", "an unknown name is not a crash"


def test_ask_has_a_timeout_in_seconds(monkeypatch):
    monkeypatch.delenv("PROVLEDGER_ASK_RUNNER", raising=False)
    assert cli.build_parser().parse_args(["ask", "q"]).timeout == 180.0
    assert cli.build_parser().parse_args(["ask", "q", "--timeout", "12"]).timeout == 12.0


def test_cli_runner_none_is_the_no_model_path(monkeypatch):
    monkeypatch.delenv("PROVLEDGER_ASK_RUNNER", raising=False)
    args = cli.build_parser().parse_args(["ask", "q", "--runner", "none"])
    assert cli._ask_runner(args) == (None, "none")


def test_cli_ask_prints_the_real_reason_not_no_model(conn, graph, seeded, tmp_path, capsys, monkeypatch):
    import shutil
    conn.commit()
    db_path = tmp_path / "orch.db"
    shutil.copy(conn.execute("PRAGMA database_list").fetchone()[2], db_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": [{"name": "proj", "db_path": graph, "repo": str(tmp_path)}]}))
    monkeypatch.setenv("ORCH_DB", str(db_path))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("PROVLEDGER_ASK_RUNNER", "stub")
    assert cli.main(["ask", QUESTION, "--project", "proj"]) == 0
    out = capsys.readouterr().out
    assert "summary unavailable: model returned nothing" in out
    assert "no model configured" not in out


# ── the runner protocol itself ───────────────────────────────────────────────

def test_a_runner_may_return_text_or_text_and_detail():
    assert R.normalise("hi") == ("hi", {})
    assert R.normalise(None) == ("", {})
    assert R.normalise(("hi", {"rc": 0})) == ("hi", {"rc": 0})
    assert R.normalise(("", None)) == ("", {})


def test_call_classifies_every_outcome():
    ok = R.call(lambda p, *, model=None, timeout_s=None: "text", "p")
    assert ok[2] == "ok" and ok[0] == "text" and ok[1]["raw_len"] == 4
    empty = R.call(lambda p, *, model=None, timeout_s=None: ("  ", {"rc": 2}), "p")
    assert empty[2] == "empty" and empty[1]["rc"] == 2
    failed = R.call(lambda p, *, model=None, timeout_s=None: (_ for _ in ()).throw(ValueError("x")), "p")
    assert failed[2] == "failed" and failed[1]["error"] == "ValueError: x"

    def slow(p, *, model=None, timeout_s=None):
        raise R.RunnerTimeout("out of time", {"timeout_s": 5})

    out = R.call(slow, "p", timeout_s=5)
    assert out[2] == "timeout" and out[1]["timeout_s"] == 5


def test_call_passes_the_timeout_through():
    seen = {}

    def spy(p, *, model=None, timeout_s=None):
        seen["timeout_s"] = timeout_s
        seen["model"] = model
        return "x"

    R.call(spy, "p", model="m", timeout_s=9.5)
    assert seen == {"timeout_s": 9.5, "model": "m"}


# ── the answer is in the language the reader asked in ────────────────────────
# `--lang` / `?lang=` switched the scope line and nothing else, so a model that
# felt like answering in Chinese did, against a prompt that asks for English,
# and no check noticed. Now it is a drop, counted like every other drop.

def test_a_chinese_sentence_is_dropped_from_an_english_answer(conn, ft, seeded):
    rid = seeded["reason"]
    answer = (f"The column was dropped because the upstream feed stopped providing it [#{rid}]. "
              f"上游在 v2 之后不再提供该列 [#{rid}].")
    got = SU.summarize(QUESTION, ft, runner=_runner(answer), candidates=3)
    assert len(got["sentences"]) == 1 and "上游" not in got["answer"]
    assert got["dropped"]["language"] == 1
    assert got["dropped_detail"][0]["reason"] == "language"
    assert "not in the language asked for" in got["note"]


def test_the_same_sentence_is_kept_when_the_reader_asked_in_chinese(conn, ft, seeded):
    rid = seeded["reason"]
    answer = f"上游在 v2 之后不再提供该列 [#{rid}]."
    assert SU.summarize(QUESTION, ft, runner=_runner(answer), candidates=3, lang="zh")["sentences"]
    assert SU.summarize(QUESTION, ft, runner=_runner(answer), candidates=3, lang="en")["sentences"] == []


def test_japanese_and_korean_count_as_not_english(conn, ft, seeded):
    rid = seeded["reason"]
    for s in (f"この列は削除されました [#{rid}].", f"이 컬럼은 삭제되었습니다 [#{rid}]."):
        got = SU.summarize(QUESTION, ft, runner=_runner(s), candidates=3)
        assert got["sentences"] == [] and got["dropped"]["language"] == 1


def test_ask_run_passes_the_language_down_to_the_check(conn, graph, seeded):
    rid = seeded["reason"]

    def spy(prompt, *, model=None, timeout_s=None):
        if "picking which nodes" in prompt:
            return '{"chosen": ["pkg.rollup.discount_rate"], "basis": "named"}'
        return f"上游不再提供该列 [#{rid}]."

    en = ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph, runner=spy, record=False, lang="en")
    zh = ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph, runner=spy, record=False, lang="zh")
    assert en["answer"] == "" and en["dropped"]["language"] == 1
    assert zh["answer"] and zh["dropped"]["language"] == 0


def test_the_prompt_asks_for_english_first_and_shows_one(conn):
    text = SU.prompt_text()
    rules = text.split("## Hard rules", 1)[1]
    assert rules.index("English") < rules.index("cites") if "cites" in rules else True
    first_rule = rules.split("\n2.", 1)[0]
    assert "English" in first_rule, "the language rule is the first rule"
    assert "[#3]" in first_rule and "[#r2]" in first_rule, "the first rule carries an English example sentence"
