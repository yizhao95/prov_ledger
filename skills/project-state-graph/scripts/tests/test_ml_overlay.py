"""RED tests for analyzer.ml_overlay — ML training concepts as graph nodes.

Node types:
  split          : a train/test/validation partition (role in metadata)
  model          : an ML model artifact
  hyperparameter : a tuning knob (name, value, options)

Edges:
  splits_into : dataset/dataframe-ish source -> split   (from train_test_split)
  trains      : split -> model                          (from .fit / .train)
  tunes       : hyperparameter -> model                 (from param grids/config)
"""
import json

import pytest

from analyzer import ml_overlay, py_ast, store, walker


def _analyze(tmp_path, source: str):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(source)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    ml_overlay.analyze(conn, str(repo), fm)
    return conn


def _nodes(conn, type_name):
    return conn.execute(
        """SELECT n.id, n.name, n.metadata_json
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


def _edges(conn, type_name):
    return conn.execute(
        """SELECT s.name, d.name, e.metadata_json
           FROM edge e
           JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node s ON e.src_node_id=s.id
           JOIN node d ON e.dst_node_id=d.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


def test_train_test_split_creates_split_nodes(tmp_path):
    src = (
        "def train():\n"
        "    X_train, X_test = train_test_split(data)\n"
    )
    conn = _analyze(tmp_path, src)
    roles = {json.loads(m or "{}").get("role") for _, _, m in _nodes(conn, "split")}
    assert "train" in roles
    assert "test" in roles


def test_validation_split_role(tmp_path):
    src = (
        "def train():\n"
        "    X_train, X_val, X_test = train_test_split(data)\n"
    )
    conn = _analyze(tmp_path, src)
    roles = {json.loads(m or "{}").get("role") for _, _, m in _nodes(conn, "split")}
    assert "validation" in roles


def test_fit_creates_model_and_trains_edge(tmp_path):
    src = (
        "def train():\n"
        "    X_train, X_test = train_test_split(data)\n"
        "    model.fit(X_train)\n"
    )
    conn = _analyze(tmp_path, src)
    assert _nodes(conn, "model"), "expected a model node"
    assert _edges(conn, "trains"), "expected a trains edge split->model"


def test_param_grid_creates_hyperparameters_and_tunes(tmp_path):
    src = (
        "def train():\n"
        "    param_grid = {'max_depth': [3, 5], 'lr': [0.1, 0.01]}\n"
        "    model.fit(X)\n"
    )
    conn = _analyze(tmp_path, src)
    hp = {n for _, n, _ in _nodes(conn, "hyperparameter")}
    assert {"max_depth", "lr"} <= hp
    # options captured
    metas = {n: json.loads(m or "{}") for _, n, m in _nodes(conn, "hyperparameter")}
    assert metas["max_depth"].get("options") == [3, 5]
    assert _edges(conn, "tunes"), "expected tunes edges hyperparameter->model"
