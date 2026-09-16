"""ask.facts / ask.scope — the fact table and the scope line (DP phase 2e, Task 1; spec §21, J4).

Nothing in here is written by a model: every row is a query result, and every
number in the scope line is a count of what the queries returned.
"""
import json
import sys
from pathlib import Path

import pytest

from orchestrator import provenance as pv
from orchestrator.ask import facts as F, scope as S

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402


@pytest.fixture
def graph(tmp_path):
    """One pipeline node, two runs: added in run 1, signature changed in run 2."""
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_snapshot(c, 1, "nk_p", "pkg.pipe.build_features")
    ps.add_event(c, 1, 1, "node_added", "nk_p", created_at="2026-09-01T09:00:00+00:00")
    ps.add_run(c, 2, plan_id="P1")
    ps.add_snapshot(c, 2, "nk_p", "pkg.pipe.build_features", struct_sig="s2")
    ps.add_event(c, 2, 1, "node_matched", "nk_p", created_at="2026-09-10T09:00:00+00:00")
    ps.add_event(c, 2, 2, "signature_changed", "nk_p", '{"struct_sig": "s2"}', created_at="2026-09-10T09:00:01+00:00")
    c.commit(); c.close()
    return str(path)


@pytest.fixture
def seeded(conn, graph):
    """A pipeline node with one stated constraint, two influence rows and no outcome."""
    ids = {}
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0",
                            text="build_features must drop rows with a null label", occurred_at="2026-09-01 10:00:00")
    ids["utterance"] = u
    ids["constraint"] = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_p", kind="organizational",
                                         role="constraint", verbatim=(u, 0, len("build_features must drop rows with a null label")),
                                         recorded_by="human", occurred_at="2026-09-01 10:00:00")
    ids["reference"] = pv.insert_reference(conn, project="proj", kind="meeting", label="feature review 2026-09-01",
                                           occurred_at="2026-09-01 09:30:00")
    pv.link_reference(conn, ids["constraint"], ids["reference"])
    ids["reason"] = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_p", kind="technical",
                                     interpretation="the null-label drop moved upstream", recorded_by="agent",
                                     occurred_at="2026-09-10 11:00:00")
    for plan in ("P2", "P3"):
        conn.execute("INSERT INTO influence (reason_id, project, plan_id, node_key, via, by, at) "
                     "VALUES (?, 'proj', ?, 'nk_p', 'headline_response', 'agent', '2026-09-10 12:00:00')",
                     (ids["constraint"], plan))
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment) VALUES (?, 'proj', 'P2', 'plan')", (ids["constraint"],))
    ids["expectation"] = conn.execute(
        "INSERT INTO expectations (plan_id, step_id, project, target, target_kind, claim, channel) "
        "VALUES ('P1', 'P1-a', 'proj', 'pkg.pipe.build_features', 'node', 'the null-label drop keeps recall', 'metric:recall') "
        "RETURNING id").fetchone()[0]
    conn.commit()
    return ids


def test_fact_table_has_every_field_and_writes_nothing(conn, graph, seeded):
    before = conn.execute("SELECT COUNT(*) FROM read_hit").fetchone()[0]
    ft = F.facts(conn, graph, ["pkg.pipe.build_features"], project="proj")
    assert conn.execute("SELECT COUNT(*) FROM read_hit").fetchone()[0] == before, "the fact table is a read"
    n = ft["nodes"][0]
    assert n["qn"] == "pkg.pipe.build_features" and n["node_key"] == "nk_p" and n["status"] == "existing"

    con = n["constraints"][0]
    assert con["cite"] == f"#{seeded['constraint']}" and con["tier"] == "stated" and con["state"] == "active"
    assert "null label" in con["text"] and con["shown"] == 1 and sorted(con["adopted_by"]) == ["P2", "P3"]
    assert con["references"] == [{"cite": f"#r{seeded['reference']}", "kind": "meeting",
                                 "label": "feature review 2026-09-01", "uri": None}]

    assert [r["cite"] for r in n["reasons"]] == [f"#{seeded['reason']}"]
    assert len(n["influence"]) == 2 and {i["plan_id"] for i in n["influence"]} == {"P2", "P3"}
    assert all(i["cite"].startswith("#i") and i["reason_id"] == seeded["constraint"] for i in n["influence"])

    changes = n["changes"]
    assert [c["event_type"] for c in changes] == ["node_added", "signature_changed"], "node_matched is not a change"
    assert all(c["cite"].startswith("#e") for c in changes)
    assert n["first_seen"] == "2026-09-01" and n["last_changed"] == "2026-09-10"

    ex = n["expectations"][0]
    assert ex["cite"] == f"#x{seeded['expectation']}" and ex["outcome"] is None and "recall" in ex["claim"]


def test_every_cite_id_resolves_and_the_number_set_covers_the_rendering(conn, graph, seeded):
    ft = F.facts(conn, graph, ["pkg.pipe.build_features"], project="proj")
    text = F.render(ft)
    for cite in F.cites_in(text):
        assert cite in ft["ids"], f"{cite} is rendered but not citable"
    assert F.numbers_in(text) <= ft["numbers"], "a number is printed that the table does not state"
    assert F.sha(ft) == F.sha(F.facts(conn, graph, ["pkg.pipe.build_features"], project="proj"))
    assert len(F.sha(ft)) == 64
    ref = ft["ids"][f"#r{seeded['reference']}"]
    assert ref["kind"] == "reference" and ref["source_kind"] == "meeting", "the cite namespace is not shadowed by the source's own kind"
    assert ref["reason_id"] == seeded["constraint"], "a source with no uri still anchors on the record that cites it"
    assert ft["ids"][f"#{seeded['constraint']}"]["kind"] == "constraints"


def test_a_node_the_graph_does_not_know_is_reported_as_new_not_dropped(conn, graph, seeded):
    ft = F.facts(conn, graph, ["pkg.pipe.build_features", "pkg.pipe.invented"], project="proj")
    missing = [n for n in ft["nodes"] if n["qn"] == "pkg.pipe.invented"][0]
    assert missing["status"] == "new" and missing["constraints"] == [] and missing["node_key"] is None
    assert "pkg.pipe.invented" in F.render(ft)


def test_j4_scope_numbers_equal_the_fact_table(conn, graph, seeded):
    ft = F.facts(conn, graph, ["pkg.pipe.build_features"], project="proj")
    sc = S.scope(ft, candidates=7, chosen=1)
    assert sc["nodes"] == 1 == len(ft["nodes"])
    assert sc["constraints"] == sum(len(n["constraints"]) for n in ft["nodes"]) == 1
    assert sc["influencing"] == sum(len(n["influence"]) for n in ft["nodes"]) == 2
    assert sc["changes"] == sum(len(n["changes"]) for n in ft["nodes"]) == 2
    assert sc["span"] == ["2026-09-01", "2026-09-10"]
    assert sc["candidates"] == 7 and sc["chosen"] == 1 and sc["truncated"] == {}
    line = S.line(sc)
    assert line.startswith("Scope:") and "1 node" in line and "2 influencing records" in line
    assert "2026-09-01 to 2026-09-10" in line and "nothing truncated" in line
    assert json.loads(json.dumps(sc)) == sc


def test_scope_says_what_was_truncated(conn, graph, seeded):
    ft = F.facts(conn, graph, ["pkg.pipe.build_features"], project="proj")
    ft["truncated"] = {"candidates": 12}
    sc = S.scope(ft, candidates=52, chosen=1)
    assert sc["truncated"] == {"candidates": 12}
    assert "12 candidates truncated" in S.line(sc)


def test_span_is_empty_when_nothing_is_dated(conn, graph):
    ft = F.facts(conn, graph, ["pkg.pipe.nothing"], project="proj")
    sc = S.scope(ft, candidates=0, chosen=0)
    assert sc["span"] == [None, None] and "no dated record" in S.line(sc)
