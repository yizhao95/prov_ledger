"""`provledger ask submit` — the session's own model drafts, the code still checks
(DP phase 2e, Task 6; spec §21, J1 / J2).

`/ledger` is first a slash command inside a Claude Code session: there is
already a model in the room, so the skill hands it the computed fact table and
asks for a draft. That changes *who* writes the sentence and nothing else — the
same code reads it back, deletes what does not cite and what carries a number
the table never stated, and counts every deletion. A draft that is never
checked is a model talking to itself.
"""
import json
import sys
from pathlib import Path

import pytest

from orchestrator import ask, cli, provenance as pv
from orchestrator.ask import summarize as SU

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

QUESTION = "why must build_features drop null labels?"


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
def asked(conn, graph):
    """One logged question with no answer yet — exactly what the skill's step (a) leaves."""
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0",
                            text="build_features must drop rows with a null label", occurred_at="2026-09-01 10:00:00")
    cid = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_p", kind="organizational", role="constraint",
                           verbatim=(u, 0, 46), recorded_by="human", occurred_at="2026-09-01 10:00:00")
    conn.commit()
    doc = ask.run(conn, project="proj", question=QUESTION, psg_db_path=graph, runner=None)
    return {"ask_id": doc["ask_id"], "constraint": cid, "doc": doc}


@pytest.mark.veto
def test_j1_j2_apply_to_a_session_draft_exactly_as_to_a_headless_one(conn, graph, asked):
    cid = asked["constraint"]
    draft = (f"A constraint requires the null-label drop [#{cid}]. "
             f"I think this was a data quality decision. "
             f"It removed 4127 rows last week [#{cid}]. "
             f"The decision is recorded [#999999].")
    out = ask.submit(conn, asked["ask_id"], draft, psg_db_path=graph)
    assert out["sentences"] == [f"A constraint requires the null-label drop [#{cid}]."]
    assert "4127" not in out["answer"] and "I think" not in out["answer"]
    assert out["dropped"] == {**SU.no_drops(), "uncited": 1, "unknown_id": 1, "number": 1}
    assert out["cites"] == [f"#{cid}"] and out["version"] == 1 and out["model"] == "session"


def test_a_second_draft_is_a_new_version_never_an_update(conn, graph, asked):
    cid = asked["constraint"]
    first = ask.submit(conn, asked["ask_id"], f"A constraint requires it [#{cid}].", psg_db_path=graph)
    second = ask.submit(conn, asked["ask_id"], f"Finance asked for it [#{cid}].", psg_db_path=graph)
    assert first["version"] == 1 and second["version"] == 2
    rows = conn.execute("SELECT version, answer, model FROM ask_answer WHERE ask_id = ? ORDER BY version",
                        (asked["ask_id"],)).fetchall()
    assert [r[0] for r in rows] == [1, 2] and "Finance" in rows[1][1]
    assert "A constraint requires it" in rows[0][1], "the first draft is still on the record"
    assert ask.latest_answer(conn, asked["ask_id"])["version"] == 2


def test_submit_refuses_an_ask_id_that_was_never_logged(conn, graph):
    with pytest.raises(ValueError, match="no logged question"):
        ask.submit(conn, 424242, "anything [#1].", psg_db_path=graph)


def test_submit_rebuilds_the_same_fact_table_the_draft_saw(conn, graph, asked):
    out = ask.submit(conn, asked["ask_id"], f"A constraint requires it [#{asked['constraint']}].", psg_db_path=graph)
    assert out["facts_sha"] == asked["doc"]["facts_sha"], "a draft is checked against the table it was given"
    assert out["scope_line"] == asked["doc"]["scope_line"]
    assert out["absences"] == asked["doc"]["absences"]


def test_the_cli_submits_from_a_file_and_prints_the_two_next_commands(conn, graph, asked, tmp_path, capsys, monkeypatch):
    import shutil
    conn.commit()
    db_path = tmp_path / "orch.db"
    shutil.copy(conn.execute("PRAGMA database_list").fetchone()[2], db_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": [{"name": "proj", "db_path": graph, "repo": str(tmp_path)}]}))
    monkeypatch.setenv("ORCH_DB", str(db_path))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(registry))
    draft = tmp_path / "draft.md"
    cid = asked["constraint"]
    draft.write_text(f"A constraint requires the null-label drop [#{cid}]. And this one guesses.\n")
    assert cli.main(["ask", "submit", str(asked["ask_id"]), "--answer-file", str(draft)]) == 0
    out = capsys.readouterr().out
    assert f"[#{cid}]" in out and "guesses" not in out
    assert "1 sentence(s) dropped" in out and "Scope:" in out
    assert "/node/proj/pkg.pipe.build_features?at=reason:" in out
    assert f"provledger why pkg.pipe.build_features" in out
    assert f"provledger ask card {asked['ask_id']} --out card.md" in out


def test_the_cli_writes_the_card_for_a_logged_question(conn, graph, asked, tmp_path, capsys, monkeypatch):
    import shutil
    conn.commit()
    db_path = tmp_path / "orch.db"
    shutil.copy(conn.execute("PRAGMA database_list").fetchone()[2], db_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": [{"name": "proj", "db_path": graph, "repo": str(tmp_path)}]}))
    monkeypatch.setenv("ORCH_DB", str(db_path))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(registry))
    out_file = tmp_path / "card.md"
    assert cli.main(["ask", "card", str(asked["ask_id"]), "--out", str(out_file)]) == 0
    text = out_file.read_text(encoding="utf-8")
    assert text.startswith("# Evidence card") and "Integrity at export time" in text
    assert cli.main(["ask", "card", "424242", "--out", str(out_file)]) == 2


def test_a_fact_table_that_moved_under_the_draft_is_said_out_loud(conn, graph, asked):
    """Found on the live ledger: the project graph finished rebuilding between
    `ask` and `ask submit`, so the table the draft was written from and the table
    it was checked against were not the same one. Checking silently against a
    newer table is the failure this whole feature exists to prevent."""
    cid = asked["constraint"]
    pv.insert_reason(conn, project="proj", plan_id="P9", node_key="nk_p", kind="technical",
                     interpretation="a record that landed after the question was asked", recorded_by="agent")
    conn.commit()
    out = ask.submit(conn, asked["ask_id"], f"A constraint requires it [#{cid}].", psg_db_path=graph)
    assert out["facts_changed"] is True
    assert out["facts_sha"] != asked["doc"]["facts_sha"]
    assert "changed between the question and this answer" in ask.render_text(out)
    same = ask.submit(conn, asked["ask_id"], f"A constraint requires it [#{cid}].", psg_db_path=graph)
    assert same["facts_changed"] is True, "still not the table the question was logged with"
