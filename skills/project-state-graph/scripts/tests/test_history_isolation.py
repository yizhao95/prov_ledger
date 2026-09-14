"""Shadow discipline (spec §5 / Task 11): the history layer must be invisible
to the graph it observes. Building with history.snapshot_run / resolve and
cards.attach_history replaced by no-ops must yield row-for-row identical
node / edge / consistency_card contents (node_key and run ids aside)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from analyzer import cards, cli, history

ROOT = Path(__file__).resolve().parents[4]          # tests -> scripts -> project-state-graph -> skills -> repo root
REPOS = {
    "phantom-uplift": ROOT / "examples" / "phantom-uplift",
    "corpus-basic_pipeline": __import__("tests.corpus.harness", fromlist=["default_corpus"]).default_corpus() / "basic_pipeline" / "base",
}


def _build(repo: Path, db: Path, monkeypatch=None) -> None:
    if monkeypatch is not None:
        monkeypatch.setattr(history, "snapshot_run", lambda conn, root, run_id, **kw: 0)   # phase 6: providers=, report=, file_map=
        monkeypatch.setattr(history, "resolve", lambda conn, run_id, arbitrate=None: {})
        monkeypatch.setattr(cards, "attach_history", lambda conn, limit=10: 0)
    cli.run(str(repo), "iso", str(db))


def _projection(db: Path) -> dict:
    c = sqlite3.connect(str(db))
    nodes = sorted(c.execute(
        """SELECT t.name, n.name, n.qualified_name, n.file_path, n.line_start, n.line_end,
                  n.metadata_json, n.dtype, n.dtype_provenance, n.data_class, n.nullable
           FROM node n JOIN node_type t ON n.node_type_id=t.id""").fetchall(), key=repr)
    edges = sorted(c.execute(
        """SELECT t.name, COALESCE(s.qualified_name, s.name), s.line_start, COALESCE(d.qualified_name, d.name), d.line_start,
                  e.metadata_json, e.confidence
           FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node s ON s.id=e.src_node_id JOIN node d ON d.id=e.dst_node_id""").fetchall(), key=repr)
    ccards = sorted((qn, json.loads(cj)) for qn, cj in c.execute(
        "SELECT n.qualified_name, cc.card_json FROM consistency_card cc JOIN node n ON n.id=cc.symbol_id"))
    scards = {}
    for qn, cj in c.execute("SELECT qualified_name, card_json FROM symbol_card"):
        card = json.loads(cj)
        card.pop("node_key", None); card.pop("history", None)   # the only fields the history layer adds
        scards[qn] = card
    c.close()
    return {"node": nodes, "edge": edges, "consistency_card": ccards, "symbol_card": scards}


@pytest.mark.parametrize("name", sorted(REPOS), ids=sorted(REPOS))
def test_history_layer_is_invisible_to_the_graph(name, tmp_path, monkeypatch):
    repo = REPOS[name]
    assert repo.exists(), repo
    with_hist = tmp_path / "with-state-graph.db"
    _build(repo, with_hist)
    without = tmp_path / "without-state-graph.db"
    with monkeypatch.context() as m:
        _build(repo, without, m)
    a, b = _projection(with_hist), _projection(without)
    for table in ("node", "edge", "consistency_card", "symbol_card"):
        assert a[table] == b[table], f"{name}: {table} differs with/without the history layer"
    assert a["node"], f"{name}: empty graph"
    # and the history layer really ran in the first build
    c = sqlite3.connect(str(with_hist))
    assert c.execute("SELECT COUNT(*) FROM node_snapshot").fetchone()[0] > 0
    assert c.execute("SELECT COUNT(*) FROM node WHERE node_key IS NOT NULL").fetchone()[0] > 0
    assert sqlite3.connect(str(without)).execute("SELECT COUNT(*) FROM node_snapshot").fetchone()[0] == 0
