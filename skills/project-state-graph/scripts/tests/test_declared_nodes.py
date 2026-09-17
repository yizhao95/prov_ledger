"""DP phase 2c: a declaration is built into the graph, and from then on its
changes are computed exactly like a function's.

The analyzer's declared stage reads the ACTIVE declarations of this project out
of the orchestrator database and writes one node plus its declared_* edges; the
`provledger.declared` provider projects those rows into node_snapshot; the
history layer does the rest. So editing a declaration's attributes is a
node_changed, and retiring it is a node_removed — nobody writes either event by
hand.
"""
import json
from pathlib import Path

import pytest

from analyzer import (data_model, dataflow, dataflow_types, history, py_ast,
                      sql_refs, store, walker)

PROJECT = "demo"
BASE = {"pkg/__init__.py": "", "pkg/m.py": "def load(path):\n    x = path + 1\n    return x\n\n"
                                           "def clean(df):\n    df = df[df.q > 0]\n    return df\n\n"
                                           "def main():\n    return clean(load('p'))\n"}


@pytest.fixture
def stage():
    """The analyzer's declared stage, imported per test so its absence is a red
    test rather than a collection error."""
    from analyzer import declared_nodes
    return declared_nodes


@pytest.fixture
def backend():
    """The backend's declared store and db module, reached the one sanctioned
    way (analyzer._host) — never spliced into sys.path here."""
    from analyzer import _host
    return _host


@pytest.fixture
def conn(tmp_path):
    c = store.init_db(str(tmp_path / "demo-state-graph.db"))
    yield c
    c.close()


@pytest.fixture
def orch(tmp_path, monkeypatch, backend):
    path = tmp_path / "orchestrator.db"
    c = backend.db.open_db(path)
    backend.db.run_migrations(c)
    monkeypatch.setenv("ORCH_DB", str(path))
    yield c
    c.close()


def _declare(backend, orch, description, *, node_type="business_rule", links=(), attrs=None, known=("pkg.m.clean",)):
    draft = backend.declared.declare(orch, PROJECT, description, node_type=node_type, links=links,
                                     attrs=attrs, known_names=set(known))
    uid = backend.provenance.insert_utterance(orch, session_id="s", project=PROJECT, plan_id=None,
                                              text=description, occurred_at="2026-03-14 09:00:00")
    return backend.declared.confirm(orch, draft["id"], uid)


def _run(conn, stage, repo: Path, files: dict, project: str = PROJECT) -> int:
    for rel, src in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    run_id = store.start_run(conn, project_name=project, commit_sha="c")
    store.reset_graph(conn)
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    data_model.analyze(conn, str(repo), fm)
    sql_refs.analyze(conn, str(repo), fm)
    stage.analyze(conn, str(repo), project)
    history.snapshot_run(conn, str(repo), run_id)
    history.resolve(conn, run_id)
    store.stamp_run(conn, run_id)
    store.finish_run(conn, run_id)
    return run_id


def _events(conn, run_id, key):
    return [r[0] for r in conn.execute(
        "SELECT event_type FROM node_event WHERE run_id = ? AND node_key = ? ORDER BY seq", (run_id, key))]


def _key(conn, run_id, qn):
    row = conn.execute("SELECT node_key FROM node_snapshot WHERE run_id = ? AND qualified_name = ?",
                       (run_id, qn)).fetchone()
    return row[0] if row else None


def test_an_active_declaration_becomes_a_node_and_its_links_become_edges(conn, orch, tmp_path, stage, backend):
    _declare(backend, orch, "EMEA excluded from the Q3 rollup",
             links=[("pkg.m.clean", "declared_constrains")], attrs={"scope": "Q3"})
    run_id = _run(conn, stage, tmp_path / "r1", BASE)
    qn = "declared:emea-excluded-from-the-q3-rollup"
    key = _key(conn, run_id, qn)
    assert key, "the declaration is not in the snapshot"
    row = conn.execute("SELECT node_type, attrs_json FROM node_snapshot WHERE run_id = ? AND qualified_name = ?",
                       (run_id, qn)).fetchone()
    assert row[0] == "business_rule"
    attrs = json.loads(row[1])
    assert attrs["type_id"] == "provledger.declared" and attrs["declared_tier"] == "stated"
    edge = conn.execute(
        "SELECT t.name, d.qualified_name FROM edge e JOIN edge_type t ON t.id = e.edge_type_id "
        "JOIN node s ON s.id = e.src_node_id JOIN node d ON d.id = e.dst_node_id WHERE s.qualified_name = ?",
        (qn,)).fetchall()
    assert edge == [("declared_constrains", "pkg.m.clean")]
    assert _events(conn, run_id, key) == ["node_added"]


def test_editing_the_attributes_is_a_node_changed_on_the_next_run(conn, orch, tmp_path, stage, backend):
    row = _declare(backend, orch, "EMEA excluded from the Q3 rollup", attrs={"scope": "Q3"})
    r1 = _run(conn, stage, tmp_path / "r1", BASE)
    qn = "declared:emea-excluded-from-the-q3-rollup"
    key = _key(conn, r1, qn)
    backend.declared.revise(orch, row["id"], attrs={"scope": "Q3 and Q4"})
    r2 = _run(conn, stage, tmp_path / "r2", BASE)
    assert _key(conn, r2, qn) == key, "the slug is the identity: a revision is the same node"
    assert "node_changed" in _events(conn, r2, key)


def test_retiring_a_declaration_is_a_node_removed(conn, orch, tmp_path, stage, backend):
    row = _declare(backend, orch, "EMEA excluded from the Q3 rollup", attrs={"scope": "Q3"})
    r1 = _run(conn, stage, tmp_path / "r1", BASE)
    key = _key(conn, r1, "declared:emea-excluded-from-the-q3-rollup")
    backend.declared.retire(orch, row["id"])
    r2 = _run(conn, stage, tmp_path / "r2", BASE)
    assert _key(conn, r2, "declared:emea-excluded-from-the-q3-rollup") is None
    assert _events(conn, r2, key) == ["node_removed"]


def test_a_repository_with_no_declarations_gains_nothing(conn, orch, tmp_path, stage, backend):
    """The declared stage on a project nobody has declared anything for must
    leave the graph byte-identical — that is why the scenario goldens hold."""
    run_id = _run(conn, stage, tmp_path / "r1", BASE)
    kinds = {r[0] for r in conn.execute("SELECT node_type FROM node_snapshot WHERE run_id = ?", (run_id,))}
    assert kinds and not kinds & set(stage.DECLARED_TYPES)
