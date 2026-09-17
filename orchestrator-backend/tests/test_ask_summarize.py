"""ask.summarize + `provledger ask` — a summary that may only say what the fact
table says (DP phase 2e, Task 2; spec §21, J1 / J2 / J7).

J1 and J2 are veto items: a sentence with no record id, and a sentence with a
number the fact table does not state, must never reach the reader.
"""
import json
import os
import sys
from pathlib import Path

import pytest

from orchestrator import ask, cli, provenance as pv
from orchestrator.ask import facts as F, summarize as SU

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

QUESTION = "why does build_features drop null labels?"


@pytest.fixture
def graph(tmp_path):
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_snapshot(c, 1, "nk_p", "pkg.pipe.build_features")
    ps.add_event(c, 1, 1, "node_added", "nk_p", created_at="2026-09-01T09:00:00+00:00")
    c.commit(); c.close()
    return str(path)


@pytest.fixture
def seeded(conn, graph):
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0",
                            text="build_features must drop rows with a null label", occurred_at="2026-09-01 10:00:00")
    cid = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_p", kind="organizational", role="constraint",
                           verbatim=(u, 0, 46), recorded_by="human", occurred_at="2026-09-01 10:00:00")
    conn.commit()
    return {"constraint": cid}


@pytest.fixture
def ft(conn, graph, seeded):
    return F.facts(conn, graph, ["pkg.pipe.build_features"], project="proj")


def _runner(answer):
    """The summarize model answers in the JSON shape the prompt demands — the
    host's plugins write prose into `result`, so prose is not the contract."""
    def run(prompt, *, model=None, timeout_s=None):
        return json.dumps({"sentences": [answer]})
    return run


@pytest.mark.veto
def test_j1_a_sentence_without_a_record_id_never_reaches_the_reader(conn, ft, seeded):
    cid = seeded["constraint"]
    answer = (f"The node drops null labels because a constraint says so [#{cid}]. "
              f"It was probably a data quality decision.")
    got = SU.summarize(QUESTION, ft, runner=_runner(answer))
    assert "probably" not in got["answer"]
    assert got["dropped"]["uncited"] == 1 and len(got["sentences"]) == 1
    assert got["cites"] == [f"#{cid}"]
    assert got["dropped_detail"][0]["reason"] == "uncited"


@pytest.mark.veto
def test_j2_a_number_outside_the_fact_table_takes_the_whole_sentence(conn, ft, seeded):
    cid = seeded["constraint"]
    answer = (f"A constraint requires the null-label drop [#{cid}]. "
              f"It removed 4127 rows in the last run [#{cid}].")
    got = SU.summarize(QUESTION, ft, runner=_runner(answer))
    assert "4127" not in got["answer"] and len(got["sentences"]) == 1
    assert got["dropped"]["number"] == 1
    assert got["dropped_detail"][0]["numbers"] == ["4127"]


def test_a_cite_that_does_not_exist_is_dropped(conn, ft, seeded):
    answer = "The decision is recorded [#999999]."
    got = SU.summarize(QUESTION, ft, runner=_runner(answer))
    assert got["answer"] == "" and got["dropped"]["unknown_id"] == 1


def test_scope_cites_and_table_numbers_are_allowed(conn, ft, seeded):
    cid = seeded["constraint"]
    answer = (f"`pkg.pipe.build_features` has never been verified: no outcome is recorded for it in scope. [scope] "
              f"The constraint was recorded on 2026-09-01 [#{cid}].")
    got = SU.summarize(QUESTION, ft, absences=[{"code": "never_verified", "cite": "[scope]", "node": "x",
                                                "text": "`pkg.pipe.build_features` has never been verified: no outcome is recorded for it in scope. [scope]"}],
                       scope_line="Scope: 1 node, 1 constraint, 0 influencing records, 1 change, 2026-09-01 to 2026-09-01; 3 candidates, 1 chosen; nothing truncated.",
                       runner=_runner(answer))
    assert len(got["sentences"]) == 2 and got["dropped"] == SU.no_drops()
    assert "[scope]" in got["answer"] and "2026-09-01" in got["answer"]


def test_more_than_eight_sentences_are_cut_and_counted(conn, ft, seeded):
    cid = seeded["constraint"]
    answer = " ".join(f"Sentence number x [#{cid}]." for _ in range(11))
    got = SU.summarize(QUESTION, ft, runner=_runner(answer))
    assert len(got["sentences"]) == SU.MAX_SENTENCES and got["dropped"]["over_limit"] == 3


def test_j7_without_a_model_the_fact_table_is_the_answer(conn, graph, seeded):
    doc = ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph, runner=None)
    assert doc["degraded"] is True and doc["answer"] == ""
    assert doc["note"] == SU.NO_MODEL_NOTE == "summary unavailable: no model configured"
    assert doc["degraded_reason"] == "no_model"
    assert "Fact table" in doc["facts_text"] and doc["scope_line"].startswith("Scope:")
    assert doc["chosen"]["basis"] == "fallback: no model"


def test_ask_run_stores_the_answer_the_cites_and_the_drops(conn, graph, seeded):
    cid = seeded["constraint"]
    answer = f"A constraint requires the null-label drop [#{cid}]. And this one guesses."
    doc = ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph,
                  runner=_runner(json.dumps({"chosen": ["pkg.pipe.build_features"], "basis": "named"})),
                  summary_runner=_runner(answer), model="stub-model")
    row = ask.get_ask(conn, doc["ask_id"])
    assert row["answer"] == doc["answer"] and f"#{cid}" in row["cites"]
    assert row["facts_sha"] == doc["facts_sha"] and row["dropped"]["uncited"] == 1
    assert row["scope"]["nodes"] == 1 and row["model"] == "stub-model"
    assert row["chosen"]["chosen"] == ["pkg.pipe.build_features"]


def test_cli_ask_no_model_prints_the_fact_table_the_scope_and_the_records(conn, graph, seeded, tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "orch.db"
    import shutil
    conn.commit()
    shutil.copy(conn.execute("PRAGMA database_list").fetchone()[2], db_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": [{"name": "proj", "db_path": graph, "repo": str(tmp_path)}]}))
    monkeypatch.setenv("ORCH_DB", str(db_path))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(registry))
    rc = cli.main(["ask", QUESTION, "--project", "proj", "--no-model"])
    out = capsys.readouterr().out
    assert rc == 0
    assert SU.NO_MODEL_NOTE in out and "Fact table" in out and "Scope:" in out
    assert f"#{seeded['constraint']}" in out
    assert f"/node/proj/pkg.pipe.build_features?at=reason:{seeded['constraint']}" in out, "2d typed the anchor: at=reason:<id>"
    assert os.environ["ORCH_DB"] == str(db_path)


def test_cli_ask_json_carries_everything_the_page_needs(conn, graph, seeded, tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "orch.db"
    import shutil
    conn.commit()
    shutil.copy(conn.execute("PRAGMA database_list").fetchone()[2], db_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": [{"name": "proj", "db_path": graph, "repo": str(tmp_path)}]}))
    monkeypatch.setenv("ORCH_DB", str(db_path))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(registry))
    assert cli.main(["ask", QUESTION, "--project", "proj", "--no-model", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert {"ask_id", "question", "answer", "cites", "scope", "scope_line", "absences", "facts_text",
            "dropped", "degraded", "note", "candidates", "chosen", "records"} <= set(doc)
    assert doc["records"][0]["url"].endswith(f"?at=reason:{seeded['constraint']}")
    change = [r for r in doc["records"] if r["kind"] == "change"]
    assert not change or "?at=run:" in change[0]["url"], "a change event anchors on its run, not on a reason id"
