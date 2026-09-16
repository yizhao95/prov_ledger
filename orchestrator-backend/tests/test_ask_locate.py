"""ask.locate — candidates by construction, a logged model choice among them (DP phase 2e, Task 0; spec §21)."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from orchestrator import ask, constraints, provenance as pv
from orchestrator.ask import locate

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402


@pytest.fixture
def graph(tmp_path):
    """Two nodes: pkg.etag.compute_etag (pkg/etag.py) and pkg.split.train_test_split."""
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_snapshot(c, 1, "nk_etag", "pkg.etag.compute_etag")
    ps.add_snapshot(c, 1, "nk_split", "pkg.split.train_test_split")
    ps.add_snapshot(c, 1, "nk_other", "pkg.other.unrelated")
    c.commit(); c.close()
    return str(path)


@pytest.fixture
def seeded(conn, graph):
    """One FTS-only hit, one name-only hit, one number-literal-only hit."""
    ids = {}
    # (a) text hit: the words are in the statement, the node name is not in the question
    ids["fts"] = constraints.record_constraint(
        conn, project="proj", subjects=["nk_other"],
        statement="close-time rows are hashed so a late write cannot change a published digest")
    # (b) name hit: nothing in the ledger text, only the graph name matches
    ids["name_reason"] = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_etag",
                                          kind="technical", interpretation="folded the header in", recorded_by="agent")
    # (c) literal hit: the split ratio appears verbatim in a statement
    ids["literal"] = pv.insert_reason(conn, project="proj", plan_id="P2", node_key="nk_split", kind="technical",
                                      interpretation="the 80/20 ratio comes from the 2026 review", recorded_by="agent")
    conn.commit()
    return ids


QUESTION = "why does compute_etag hash close-time rows, and where does 80/20 come from?"


def test_candidates_carry_the_three_match_kinds_and_a_why_each(conn, graph, seeded):
    cands = locate.candidates(conn, graph, QUESTION, project="proj")
    by_qn = {c["qn"]: c for c in cands}
    assert "pkg.etag.compute_etag" in by_qn and "pkg.split.train_test_split" in by_qn and "pkg.other.unrelated" in by_qn
    assert all(c["why"] for c in cands), "every candidate says how it matched"
    assert "name match" in by_qn["pkg.etag.compute_etag"]["why"]
    assert "literal match" in by_qn["pkg.split.train_test_split"]["why"]
    assert "text match" in by_qn["pkg.other.unrelated"]["why"]
    assert by_qn["pkg.etag.compute_etag"]["node_key"] == "nk_etag"


def test_candidates_are_capped_at_forty(conn, graph, seeded):
    for i in range(60):
        pv.insert_reason(conn, project="proj", plan_id="P3", node_key=f"nk_bulk_{i}", kind="technical",
                         interpretation="close-time rows again", recorded_by="agent")
    conn.commit()
    assert len(locate.candidates(conn, graph, QUESTION, project="proj")) == locate.MAX_CANDIDATES


def test_choose_takes_the_model_answer_when_every_name_is_a_candidate(conn, graph, seeded):
    cands = locate.candidates(conn, graph, QUESTION, project="proj")

    def runner(prompt, *, model=None, timeout_s=None):
        assert "pkg.etag.compute_etag" in prompt and QUESTION in prompt
        return '{"chosen": ["pkg.etag.compute_etag"], "basis": "the question names it"}'

    got = locate.choose(QUESTION, cands, runner=runner)
    assert got["chosen"] == ["pkg.etag.compute_etag"] and got["fallback"] is False
    assert got["basis"] == "the question names it"


def test_choose_rejects_a_name_outside_the_candidates_and_falls_back(conn, graph, seeded):
    cands = locate.candidates(conn, graph, QUESTION, project="proj")

    def runner(prompt, *, model=None, timeout_s=None):
        return '{"chosen": ["pkg.invented.node"], "basis": "made it up"}'

    got = locate.choose(QUESTION, cands, runner=runner)
    assert got["fallback"] is True and got["basis"] == "fallback: top by match"
    assert "pkg.invented.node" not in got["chosen"] and got["chosen"]
    assert got["rejected"] == "a name outside the candidates"


def test_choose_falls_back_on_non_json_and_without_a_runner(conn, graph, seeded):
    cands = locate.candidates(conn, graph, QUESTION, project="proj")
    bad = locate.choose(QUESTION, cands, runner=lambda p, *, model=None, timeout_s=None: "sure, here you go")
    assert bad["fallback"] is True and len(bad["chosen"]) <= locate.FALLBACK_TOP
    none = locate.choose(QUESTION, cands, runner=None)
    assert none["fallback"] is True and none["basis"] == "fallback: no model"


def test_choose_caps_the_model_at_eight_nodes(conn, graph, seeded):
    cands = [{"qn": f"pkg.m.n{i}", "node_key": f"nk_{i}", "why": "name match", "score": 1} for i in range(12)]
    picked = json.dumps({"chosen": [c["qn"] for c in cands], "basis": "all of them"})
    got = locate.choose("q", cands, runner=lambda p, *, model=None, timeout_s=None: picked)
    assert got["fallback"] is True and got["rejected"] == "more than 8 nodes"


def test_ask_log_row_records_candidates_and_the_choice(conn, graph, seeded):
    cands = locate.candidates(conn, graph, QUESTION, project="proj")
    chosen = locate.choose(QUESTION, cands, runner=lambda p, *, model=None, timeout_s=None:
                           '{"chosen": ["pkg.etag.compute_etag"], "basis": "named"}')
    ask_id = ask.record_ask(conn, project="proj", question=QUESTION, candidates=cands, chosen=chosen,
                            model="stub", runner="stub")
    row = conn.execute("SELECT project, question, candidates_json, chosen_json, model, runner FROM ask_log WHERE id = ?",
                       (ask_id,)).fetchone()
    assert row[0] == "proj" and row[1] == QUESTION
    assert [c["qn"] for c in json.loads(row[2])] == [c["qn"] for c in cands]
    assert json.loads(row[3])["chosen"] == ["pkg.etag.compute_etag"]
    assert row[4] == "stub" and row[5] == "stub"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE ask_log SET answer = 'x' WHERE id = ?", (ask_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM ask_log WHERE id = ?", (ask_id,))


def test_migration_023_creates_both_tables_with_the_feedback_check(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"ask_log", "ask_feedback"} <= names
    ask_id = ask.record_ask(conn, project="proj", question="q", candidates=[], chosen={"chosen": [], "basis": "b"})
    fid = ask.record_feedback(conn, ask_id=ask_id, verdict="wrong", note="the node is the other one")
    assert tuple(conn.execute("SELECT verdict, ask_id FROM ask_feedback WHERE id = ?", (fid,)).fetchone()) == ("wrong", ask_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ask_feedback (ask_id, verdict) VALUES (?, 'maybe')", (ask_id,))
