"""Tests for analyzer.slice_viz — provLedger Phase B DataFrame-aware slices.

Four per-perspective slice builders + a self-contained HTML writer + the CLI.
This file starts with build_dataflow (column/variable-granularity dtype slice)
and grows with each slice in later steps.

slice_viz is stdlib-only (sqlite3 + json) and imports NOTHING from the analyzer
package, so it stays byte-portable into the reviewer skill (same rule as
graph_viz.py).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from analyzer import slice_viz


# ── shared graph fixture helpers ─────────────────────────────────────────────────

def _new_db(path):
    c = sqlite3.connect(str(path))
    c.executescript(
        """
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, line_start INTEGER,
                           line_end INTEGER, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT);
        """
    )
    return c


def _nt(c, name):
    cur = c.execute("INSERT OR IGNORE INTO node_type (name) VALUES (?)", (name,))
    return c.execute("SELECT id FROM node_type WHERE name=?", (name,)).fetchone()[0]


def _et(c, name):
    c.execute("INSERT OR IGNORE INTO edge_type (name) VALUES (?)", (name,))
    return c.execute("SELECT id FROM edge_type WHERE name=?", (name,)).fetchone()[0]


def _node(c, ntype, name, *, qname=None, fp=None, meta=None):
    tid = _nt(c, ntype)
    cur = c.execute(
        "INSERT INTO node (node_type_id, name, qualified_name, file_path, metadata_json) "
        "VALUES (?,?,?,?,?)",
        (tid, name, qname or name, fp, json.dumps(meta) if meta is not None else None),
    )
    return cur.lastrowid


def _edge(c, etype, src, dst, *, meta=None):
    tid = _et(c, etype)
    c.execute(
        "INSERT INTO edge (edge_type_id, src_node_id, dst_node_id, metadata_json) "
        "VALUES (?,?,?,?)",
        (tid, src, dst, json.dumps(meta) if meta is not None else None),
    )


@pytest.fixture
def dataflow_db(tmp_path):
    """A tiny graph: one typed param var, one unknown param var, one return var,
    wired with feeds/produces/consumes edges."""
    db = tmp_path / "g.db"
    c = _new_db(db)
    fn = _node(c, "function", "draw", qname="mod.draw", fp="mod.py")
    cons = _node(c, "function", "consumer", qname="mod.consumer", fp="mod.py")
    v_typed = _node(c, "data_var", "draw:param:image", fp="mod.py",
                    meta={"dtype": "np.ndarray", "dtype_provenance": "annotation",
                          "role": "param"})
    v_unknown = _node(c, "data_var", "draw:param:mystery", fp="mod.py",
                      meta={"dtype": "unknown", "role": "param"})
    v_nometa = _node(c, "data_var", "draw:param:nometa", fp="mod.py")  # no metadata
    ret = _node(c, "data_var", "draw:return", fp="mod.py",
                meta={"dtype": "dict", "role": "return"})
    _edge(c, "feeds", v_typed, fn, meta={"dtype": "np.ndarray", "role": "param"})
    _edge(c, "feeds", v_unknown, fn, meta={"dtype": "unknown", "role": "param"})
    _edge(c, "feeds", v_nometa, fn)
    _edge(c, "produces", fn, ret, meta={"type": "dict"})
    _edge(c, "consumes", ret, cons)
    c.commit()
    c.close()
    return str(db)


# ── build_dataflow ───────────────────────────────────────────────────────────────

def test_dataflow_marks_unknown_and_typed(dataflow_db):
    out = slice_viz.build_dataflow(dataflow_db)
    by_label = {n["label"].rstrip(" ?"): n for n in out["nodes"]}
    # every data node has a dtype + boolean unknown flag
    for n in out["nodes"]:
        assert "dtype" in n
        assert isinstance(n["unknown"], bool)
    typed = next(n for n in out["nodes"] if n["label"].startswith("draw:param:image"))
    unknown = next(n for n in out["nodes"] if "mystery" in n["label"])
    nometa = next(n for n in out["nodes"] if "nometa" in n["label"])
    assert typed["unknown"] is False
    assert unknown["unknown"] is True
    assert nometa["unknown"] is True  # missing dtype counts as unknown


def test_dataflow_unknown_is_gray_with_question_mark(dataflow_db):
    out = slice_viz.build_dataflow(dataflow_db)
    unknown = next(n for n in out["nodes"] if "mystery" in n["label"])
    typed = next(n for n in out["nodes"] if "image" in n["label"])
    assert unknown["color"] == "#9aa3af"          # gray sentinel
    assert unknown["label"].endswith("?")          # visible '?' marker
    assert typed["color"] != "#9aa3af"             # typed is non-gray
    assert not typed["label"].endswith("?")


def test_dataflow_emits_flow_edges_with_dtype_labels(dataflow_db):
    out = slice_viz.build_dataflow(dataflow_db)
    ets = {e["edge_type"] for e in out["edges"]}
    assert {"feeds", "produces", "consumes"} <= ets
    feed = next(e for e in out["edges"]
                if e["edge_type"] == "feeds" and e.get("label") == "np.ndarray")
    assert feed is not None


def test_dataflow_tooltip_has_dtype_and_provenance(dataflow_db):
    out = slice_viz.build_dataflow(dataflow_db)
    typed = next(n for n in out["nodes"] if "image" in n["label"])
    assert "np.ndarray" in typed["title"]
    assert "annotation" in typed["title"]


def test_dataflow_coverage_metric(dataflow_db):
    out = slice_viz.build_dataflow(dataflow_db)
    cov = out["dtype_coverage"]
    # 4 data nodes: image(typed) + return(typed) = 2 typed; mystery + nometa = 2 unknown
    assert cov["typed"] == 2
    assert cov["unknown"] == 2
    assert cov["pct"] == 50.0


def test_dataflow_empty_graph(tmp_path):
    db = tmp_path / "empty.db"
    c = _new_db(db)
    c.commit()
    c.close()
    out = slice_viz.build_dataflow(str(db))
    assert out["nodes"] == []
    assert out["edges"] == []
    assert out["dtype_coverage"]["pct"] == 0


# ── slice 2 · function call chain ────────────────────────────────────────────────

@pytest.fixture
def callchain_db(tmp_path):
    """Call graph: A->B, B->C, X->B. Focus on B sees caller A,X + callee C."""
    db = tmp_path / "cc.db"
    c = _new_db(db)
    a = _node(c, "function", "A", qname="mod.A", fp="mod.py")
    b = _node(c, "function", "B", qname="mod.B", fp="mod.py")
    cc = _node(c, "function", "C", qname="mod.C", fp="mod.py")
    x = _node(c, "function", "X", qname="mod.X", fp="mod.py")
    d = _node(c, "function", "D", qname="mod.D", fp="mod.py")  # unrelated
    _edge(c, "calls", a, b)
    _edge(c, "calls", b, cc)
    _edge(c, "calls", x, b)
    _edge(c, "calls", cc, d)  # C->D (2 hops down from B)
    c.commit()
    c.close()
    return str(db)


def test_callchain_one_hop_neighborhood(callchain_db):
    out = slice_viz.build_call_chain(callchain_db, "mod.B", hops=1)
    labels = {n["label"] for n in out["nodes"]}
    assert labels == {"A", "B", "C", "X"}  # D excluded at 1 hop
    dirs = {n["label"]: n["direction"] for n in out["nodes"]}
    assert dirs["B"] == "focus"
    assert dirs["A"] == "caller" and dirs["X"] == "caller"
    assert dirs["C"] == "callee"


def test_callchain_two_hops_includes_grandchildren(callchain_db):
    out = slice_viz.build_call_chain(callchain_db, "mod.B", hops=2)
    labels = {n["label"] for n in out["nodes"]}
    assert "D" in labels  # C->D reachable at 2 hops


def test_callchain_resolves_by_short_name(callchain_db):
    out = slice_viz.build_call_chain(callchain_db, "B", hops=1)
    assert out["focus_id"] is not None
    assert any(n["direction"] == "focus" for n in out["nodes"])


def test_callchain_edges_keep_calls_type(callchain_db):
    out = slice_viz.build_call_chain(callchain_db, "mod.B", hops=1)
    assert out["edges"]
    assert all(e["edge_type"] == "calls" for e in out["edges"])


def test_callchain_unknown_focus(callchain_db):
    out = slice_viz.build_call_chain(callchain_db, "does.not.exist", hops=1)
    assert out == {"nodes": [], "edges": [], "focus_id": None}


# ── slice 3 · pipeline view ──────────────────────────────────────────────────────

@pytest.fixture
def pipeline_db(tmp_path):
    """Two pipelines via pipeline_step edges with {index, pipeline} metadata."""
    db = tmp_path / "p.db"
    c = _new_db(db)
    load = _node(c, "function", "load", fp="m.py")
    clean = _node(c, "function", "clean", fp="m.py")
    report = _node(c, "function", "report", fp="m.py")
    a = _node(c, "function", "a", fp="m.py")
    b = _node(c, "function", "b", fp="m.py")
    # pipeline P1 inserted OUT of order to test sort-by-index
    _edge(c, "pipeline_step", clean, report, meta={"index": 1, "pipeline": "P1"})
    _edge(c, "pipeline_step", load, clean, meta={"index": 0, "pipeline": "P1"})
    # pipeline P2
    _edge(c, "pipeline_step", a, b, meta={"index": 0, "pipeline": "P2"})
    # degenerate self-step tolerated
    _edge(c, "pipeline_step", b, b, meta={"index": 1, "pipeline": "P2"})
    c.commit()
    c.close()
    return str(db)


def test_pipeline_grouped_and_ordered(pipeline_db):
    out = slice_viz.build_pipeline(pipeline_db)
    pls = {p["name"]: p for p in out["pipelines"]}
    assert set(pls) == {"P1", "P2"}
    p1_fns = [s["fn"] for s in pls["P1"]["steps"]]
    # ordered by index: step0 src=load, step1 src=clean -> sequence load, clean, report
    assert p1_fns[0] == "load"
    assert [s["index"] for s in pls["P1"]["steps"]] == sorted(s["index"] for s in pls["P1"]["steps"])


def test_pipeline_self_step_tolerated(pipeline_db):
    out = slice_viz.build_pipeline(pipeline_db)
    p2 = next(p for p in out["pipelines"] if p["name"] == "P2")
    assert any(s["fn"] == "b" for s in p2["steps"])  # did not crash on b->b


def test_pipeline_empty(tmp_path):
    db = tmp_path / "e.db"
    c = _new_db(db)
    c.commit()
    c.close()
    out = slice_viz.build_pipeline(str(db))
    assert out["pipelines"] == []


# ── slice 4 · API surface (table) ────────────────────────────────────────────────

@pytest.fixture
def api_db(tmp_path):
    """Routes via handles edges; one handler calls two functions, one calls none."""
    db = tmp_path / "api.db"
    c = _new_db(db)
    health = _node(c, "route", "/health", fp="main.py", meta={"method": "GET"})
    gen = _node(c, "route", "/generate", fp="main.py", meta={"method": "POST"})
    h_check = _node(c, "function", "health_check", qname="main.health_check", fp="main.py")
    h_gen = _node(c, "function", "generate_insights", qname="main.generate_insights", fp="main.py")
    prep = _node(c, "function", "prepare_temp_files", fp="main.py")
    proc = _node(c, "function", "process", fp="pipeline.py")
    _edge(c, "handles", health, h_check)
    _edge(c, "handles", gen, h_gen)
    _edge(c, "calls", h_gen, prep)
    _edge(c, "calls", h_gen, proc)
    c.commit()
    c.close()
    return str(db)


def test_api_surface_rows(api_db):
    rows = slice_viz.build_api_surface(api_db)
    by_path = {r["path"]: r for r in rows}
    assert set(by_path) == {"/health", "/generate"}
    assert by_path["/health"]["method"] == "GET"
    assert by_path["/generate"]["method"] == "POST"
    assert by_path["/health"]["handler"] == "health_check"
    assert by_path["/generate"]["handler"] == "generate_insights"


def test_api_surface_handler_calls(api_db):
    rows = slice_viz.build_api_surface(api_db)
    by_path = {r["path"]: r for r in rows}
    assert by_path["/generate"]["calls"] == ["prepare_temp_files", "process"]
    assert by_path["/health"]["calls"] == []


def test_api_surface_no_routes(tmp_path):
    db = tmp_path / "n.db"
    c = _new_db(db)
    c.commit()
    c.close()
    assert slice_viz.build_api_surface(str(db)) == []


# ── write_slices · self-contained HTML ───────────────────────────────────────────

@pytest.fixture
def full_db(tmp_path):
    """A graph exercising all 4 slices at once."""
    db = tmp_path / "full.db"
    c = _new_db(db)
    # dataflow
    fn = _node(c, "function", "process", qname="pipeline.process", fp="pipeline.py")
    v_typed = _node(c, "data_var", "process:param:df", fp="pipeline.py",
                    meta={"dtype": "DataFrame", "role": "param"})
    v_unknown = _node(c, "data_var", "process:param:cfg", fp="pipeline.py",
                      meta={"dtype": "unknown", "role": "param"})
    _edge(c, "feeds", v_typed, fn, meta={"dtype": "DataFrame"})
    _edge(c, "feeds", v_unknown, fn, meta={"dtype": "unknown"})
    # call chain
    helper = _node(c, "function", "helper", qname="pipeline.helper", fp="pipeline.py")
    _edge(c, "calls", fn, helper)
    # pipeline
    _edge(c, "pipeline_step", fn, helper, meta={"index": 0, "pipeline": "main"})
    # api
    rt = _node(c, "route", "/run", fp="main.py", meta={"method": "POST"})
    _edge(c, "handles", rt, fn)
    c.commit()
    c.close()
    return str(db)


def test_write_slices_creates_self_contained_file(full_db, tmp_path):
    out = tmp_path / "slices.html"
    res = slice_viz.write_slices(full_db, str(out), title="demo")
    assert res == str(out)
    assert out.exists()
    html = out.read_text()
    # no external asset refs except the vis-network CDN graph_viz already uses
    bad = [ln for ln in html.splitlines()
           if "http://" in ln or ("https://" in ln and "unpkg.com" not in ln)]
    assert bad == [], f"unexpected external refs: {bad[:3]}"


def test_write_slices_has_all_four_sections(full_db, tmp_path):
    out = tmp_path / "s.html"
    slice_viz.write_slices(full_db, str(out), title="demo")
    html = out.read_text().lower()
    for token in ("dataflow", "call", "pipeline", "api"):
        assert token in html


def test_write_slices_shows_dtype_coverage(full_db, tmp_path):
    out = tmp_path / "s.html"
    slice_viz.write_slices(full_db, str(out), title="demo")
    html = out.read_text().lower()
    assert "coverage" in html
    assert "%" in html


def test_write_slices_api_is_table(full_db, tmp_path):
    out = tmp_path / "s.html"
    slice_viz.write_slices(full_db, str(out), title="demo")
    html = out.read_text().lower()
    assert "<table" in html
    assert "/run" in out.read_text()


def test_write_slices_unknown_color_embedded(full_db, tmp_path):
    out = tmp_path / "s.html"
    slice_viz.write_slices(full_db, str(out), title="demo")
    assert "#9aa3af" in out.read_text()  # gray unknown marker present in embedded JSON
