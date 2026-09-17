"""ask.absence — the "no" sentences, computed (DP phase 2e, Task 1; spec §21, J3).

"Never verified", "no alternative was tested" and "unchanged since" are read
off the fact table by code. A model is never asked whether something is
missing, and every such sentence is marked `[scope]`: it is an absence inside
the searched range, not a claim about the world.
"""
import sys
from pathlib import Path

import pytest

from orchestrator import provenance as pv
from orchestrator.ask import absence as A, facts as F

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402


@pytest.fixture
def graph(tmp_path):
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_snapshot(c, 1, "nk_p", "pkg.pipe.build_features")
    ps.add_event(c, 1, 1, "node_added", "nk_p", created_at="2026-09-01T09:00:00+00:00")
    ps.add_run(c, 2, plan_id="P1")
    ps.add_snapshot(c, 2, "nk_p", "pkg.pipe.build_features", struct_sig="s2")
    ps.add_event(c, 2, 2, "signature_changed", "nk_p", created_at="2026-09-10T09:00:00+00:00")
    c.commit(); c.close()
    return str(path)


def _expectation(conn, claim, channel="metric:recall"):
    return conn.execute(
        "INSERT INTO expectations (plan_id, step_id, project, target, target_kind, claim, channel) "
        "VALUES ('P1', 'P1-a', 'proj', 'pkg.pipe.build_features', 'node', ?, ?) RETURNING id", (claim, channel)).fetchone()[0]


def _ft(conn, graph):
    return F.facts(conn, graph, ["pkg.pipe.build_features"], project="proj")


def test_j3_never_verified_fires_without_an_outcome_and_is_marked_scope(conn, graph):
    _expectation(conn, "the null-label drop keeps recall")
    conn.commit()
    got = {a["code"]: a for a in A.absences(conn, _ft(conn, graph))}
    assert "never_verified" in got
    a = got["never_verified"]
    assert a["node"] == "pkg.pipe.build_features" and a["cite"] == "[scope]"
    assert a["text"].endswith("[scope]") and "never been verified" in a["text"]
    assert "pkg.pipe.build_features" in a["text"]


def test_never_verified_is_silent_once_an_outcome_exists(conn, graph):
    eid = _expectation(conn, "the null-label drop keeps recall")
    conn.execute("INSERT INTO outcomes (expectation_id, kind, value_json, source, tier) "
                 "VALUES (?, 'observed', '{\"signal\": \"held\"}', 'metric', 'observed')", (eid,))
    conn.commit()
    assert "never_verified" not in {a["code"] for a in A.absences(conn, _ft(conn, graph))}


def test_no_alternatives_tested_fires_with_one_value_and_no_rejected_path(conn, graph):
    _expectation(conn, "recall stays at 0.90")
    conn.execute("INSERT INTO metrics (project, plan_id, name, value, source) VALUES ('proj', 'P1', 'recall', 0.90, 'run-step')")
    conn.commit()
    got = {a["code"]: a for a in A.absences(conn, _ft(conn, graph))}
    assert "no_alternatives_tested" in got
    a = got["no_alternatives_tested"]
    assert a["cite"] == "[scope]" and "no alternative" in a["text"].lower()
    assert a["value"] is not None and str(a["value"]) in a["text"]


def test_no_alternatives_tested_is_silent_when_a_path_was_rejected(conn, graph):
    _expectation(conn, "recall stays at 0.90")
    pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_p", kind="technical", role="rejected_path",
                     interpretation="a second pass over the raw table was tried and dropped", recorded_by="agent")
    conn.commit()
    assert "no_alternatives_tested" not in {a["code"] for a in A.absences(conn, _ft(conn, graph))}


def test_no_alternatives_tested_is_silent_with_a_second_measured_value(conn, graph):
    _expectation(conn, "recall stays at 0.90")
    conn.execute("INSERT INTO metrics (project, plan_id, name, value, source) VALUES ('proj', 'P1', 'recall', 0.90, 'run-step')")
    conn.execute("INSERT INTO metrics (project, plan_id, name, value, source) VALUES ('proj', 'P2', 'recall', 0.87, 'run-step')")
    conn.commit()
    assert "no_alternatives_tested" not in {a["code"] for a in A.absences(conn, _ft(conn, graph))}


def test_unchanged_since_carries_the_last_change_date(conn, graph):
    got = {a["code"]: a for a in A.absences(conn, _ft(conn, graph))}
    assert "unchanged_since" in got and "2026-09-10" in got["unchanged_since"]["text"]
    assert got["unchanged_since"]["text"].endswith("[scope]")


def test_unchanged_since_is_silent_for_a_node_with_no_change_history(conn, graph):
    ft = F.facts(conn, graph, ["pkg.pipe.invented"], project="proj")
    codes = {a["code"] for a in A.absences(conn, ft)}
    assert "unchanged_since" not in codes
    assert "not_in_graph" in codes, "a node the graph does not know is said out loud, not dropped"


def test_every_absence_sentence_is_english_and_ends_with_its_cite(conn, graph):
    _expectation(conn, "recall stays at 0.90")
    conn.commit()
    for a in A.absences(conn, _ft(conn, graph)):
        assert a["text"].endswith(f" {a['cite']}")
        assert a["text"][0].isupper() or a["text"].startswith("`")
        assert a["text"].isascii()


def test_an_unreachable_graph_is_said_out_loud_not_reported_as_a_missing_node(conn, graph):
    """A graph that cannot answer (missing file, mid-refresh journal lock) is not
    the same fact as a node that is not in the graph, and the second sentence
    would be a lie. The ledger-only absences still hold; the one that needs the
    change history does not."""
    _expectation(conn, "recall stays at 0.90")
    conn.commit()
    ft = F.facts(conn, "/nonexistent/state-graph.db", ["nk_p"], project="proj")
    got = {a["code"]: a for a in A.absences(conn, ft)}
    assert "not_in_graph" not in got
    assert "graph_unavailable" in got
    assert "could not be read" in got["graph_unavailable"]["text"]
    assert got["graph_unavailable"]["text"].endswith("[scope]")
    assert "never_verified" in got, "outcomes live in the ledger, not in the graph"
    assert "unchanged_since" not in got, "there is no change history to speak for"
