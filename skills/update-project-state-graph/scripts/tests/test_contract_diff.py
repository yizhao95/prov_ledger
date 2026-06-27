"""Tests for contract_diff.py — the shared base-vs-head AST fingerprint engine.

Phase A of provLedger. This module is the contract-drift engine wired into the
update-project-state-graph reviewer. The engine parses the FULL AST of each side
of a diff (never regex on diff text), extracts structured fingerprints, and
compares them to classify signature / return-contract changes.

This file starts with the ENGINE-only tests (step B). Verdict-against-graph tests
(A-1/A-2/A-3) are appended in later steps.
"""
from __future__ import annotations

import contract_diff


# ── extract_python_signatures ───────────────────────────────────────────────────

def test_extract_simple_function():
    src = "def foo(a, b):\n    return a + b\n"
    fps = contract_diff.extract_python_signatures(src)
    assert "foo" in fps
    fp = fps["foo"]
    assert fp["qualified_name"] == "foo"
    assert fp["positional"] == ["a", "b"]
    assert fp["star_args"] is False
    assert fp["kwstar"] is False
    assert fp["returns"] is None


def test_extract_with_annotations_and_defaults():
    src = (
        "def bar(x: int, y: str = 'z', *args, k: float = 1.0, **kw) -> dict:\n"
        "    return {}\n"
    )
    fps = contract_diff.extract_python_signatures(src)
    fp = fps["bar"]
    assert fp["positional"] == ["x", "y"]
    assert fp["star_args"] is True
    assert fp["kwstar"] is True
    assert "k" in fp["kwonly"]
    assert fp["annotations"]["x"] == "int"
    assert fp["annotations"]["y"] == "str"
    assert fp["returns"] == "dict"


def test_extract_methods_qualified_by_class():
    src = (
        "class C:\n"
        "    def m(self, a):\n"
        "        return a\n"
    )
    fps = contract_diff.extract_python_signatures(src)
    assert "C.m" in fps
    assert fps["C.m"]["positional"] == ["self", "a"]


def test_extract_decorated_and_multiline_signature():
    src = (
        "import functools\n"
        "@functools.cache\n"
        "def deco(\n"
        "    a: int,\n"
        "    b: int,\n"
        ") -> int:\n"
        "    return a + b\n"
    )
    fps = contract_diff.extract_python_signatures(src)
    assert "deco" in fps
    assert fps["deco"]["positional"] == ["a", "b"]
    assert fps["deco"]["returns"] == "int"


def test_body_only_change_yields_identical_fingerprint():
    base = "def f(a, b) -> int:\n    return a + b\n"
    head = "def f(a, b) -> int:\n    total = a + b\n    return total\n"
    fb = contract_diff.extract_python_signatures(base)["f"]
    fh = contract_diff.extract_python_signatures(head)["f"]
    assert fb == fh


def test_malformed_source_returns_empty_dict():
    assert contract_diff.extract_python_signatures("def broken(:\n    pass") == {}
    assert contract_diff.extract_python_signatures("???not python???") == {}


# ── compare_signatures ──────────────────────────────────────────────────────────

def _fp(src):
    return contract_diff.extract_python_signatures(src)


def test_compare_detects_param_added():
    base = _fp("def f(a):\n    return a\n")
    head = _fp("def f(a, b):\n    return a\n")
    changes = contract_diff.compare_signatures(base, head)
    kinds = {c["change_kind"] for c in changes if c["qualified_name"] == "f"}
    assert "signature_changed" in kinds


def test_compare_detects_param_removed():
    base = _fp("def f(a, b):\n    return a\n")
    head = _fp("def f(a):\n    return a\n")
    changes = contract_diff.compare_signatures(base, head)
    assert any(c["qualified_name"] == "f" and c["change_kind"] == "signature_changed"
               for c in changes)


def test_compare_detects_param_renamed():
    base = _fp("def f(a):\n    return a\n")
    head = _fp("def f(z):\n    return z\n")
    changes = contract_diff.compare_signatures(base, head)
    assert any(c["change_kind"] == "signature_changed" for c in changes)


def test_compare_detects_param_reordered():
    base = _fp("def f(a, b):\n    return a\n")
    head = _fp("def f(b, a):\n    return a\n")
    changes = contract_diff.compare_signatures(base, head)
    assert any(c["change_kind"] == "signature_changed" for c in changes)


def test_compare_detects_return_annotation_change():
    base = _fp("def f(a) -> list:\n    return a\n")
    head = _fp("def f(a) -> dict:\n    return a\n")
    changes = contract_diff.compare_signatures(base, head)
    assert any(c["qualified_name"] == "f" and c["change_kind"] == "return_contract_changed"
               for c in changes)


def test_compare_ignores_body_only_change():
    base = _fp("def f(a) -> int:\n    return a\n")
    head = _fp("def f(a) -> int:\n    x = a\n    return x\n")
    changes = contract_diff.compare_signatures(base, head)
    assert changes == []


def test_compare_ignores_function_present_on_one_side_only():
    # A removed/added function is a rename concern handled elsewhere, NOT a
    # signature change. compare_signatures only reports same-name diffs.
    base = _fp("def f(a):\n    return a\n")
    head = _fp("def g(a):\n    return a\n")
    changes = contract_diff.compare_signatures(base, head)
    assert changes == []


# ── A-1 signature verdict against a deep graph ───────────────────────────────────

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest


def _git_cmd(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git_cmd(r, "init", "-q")
    _git_cmd(r, "config", "user.email", "t@t.com")
    _git_cmd(r, "config", "user.name", "t")
    return r


def _commit(repo: Path, msg: str) -> str:
    _git_cmd(repo, "add", "-A")
    _git_cmd(repo, "commit", "-qm", msg)
    return _git_cmd(repo, "rev-parse", "HEAD")


def _graph_with_card(db_path: Path, *, fn_qname: str, fn_file: str,
                     callers: list[dict], output_consumers: list[dict]):
    """Build a tiny deep graph: a function node + its consistency_card, plus a
    node per caller/consumer so the verdict can resolve each one's file_path.

    callers / output_consumers: [{"name": str, "file": str}]
    """
    c = sqlite3.connect(str(db_path))
    c.executescript("""
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, line_start INTEGER,
                           line_end INTEGER, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT);
        CREATE TABLE consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
    """)
    c.execute("INSERT INTO node_type (id, name) VALUES (1,'function')")
    short = fn_qname.split(".")[-1]
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path) "
              "VALUES (1, 1, ?, ?, ?)", (short, fn_qname, fn_file))
    nid = 2
    for grp in (callers, output_consumers):
        for item in grp:
            c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path) "
                      "VALUES (?, 1, ?, ?, ?)",
                      (nid, item["name"], item["name"], item["file"]))
            nid += 1
    card = {
        "callers": sorted({i["name"] for i in callers}),
        "callees": [],
        "output_consumers": sorted({i["name"] for i in output_consumers}),
        "reads": [], "writes": [], "pipeline_membership": [],
        "dtype_map": {}, "columns_in": [], "columns_out": [],
        "lineage_upstream": [], "lineage_downstream": [], "profile": [],
    }
    c.execute("INSERT INTO consistency_card (symbol_id, card_json) VALUES (1, ?)",
              (json.dumps(card),))
    c.commit()
    c.close()


def test_signature_change_caller_not_in_diff_fails(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "mod.py").write_text("def target(a) -> int:\n    return a\n")
    (repo / "caller.py").write_text("from mod import target\n\ndef caller():\n    return target(1)\n")
    base = _commit(repo, "base")
    # change target's return annotation only; caller.py untouched (not in diff)
    (repo / "mod.py").write_text("def target(a) -> dict:\n    return {}\n")
    head = _commit(repo, "change return")

    db = tmp_path / "g.db"
    _graph_with_card(db, fn_qname="target", fn_file="mod.py",
                     callers=[{"name": "caller", "file": "caller.py"}],
                     output_consumers=[])
    rep = contract_diff.signature_contract(str(db), str(repo), base, head)
    assert rep["ok"] is False
    assert any(g.get("severity") == "fail" for g in rep["gaps"])
    assert any("caller" in g.get("caller", "") or "caller" in g.get("detail", "")
               for g in rep["gaps"])


def test_signature_change_caller_in_diff_is_warning(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "mod.py").write_text("def target(a) -> int:\n    return a\n")
    (repo / "caller.py").write_text("from mod import target\n\ndef caller():\n    return target(1)\n")
    base = _commit(repo, "base")
    # change BOTH: target signature AND caller.py (so caller IS in the diff)
    (repo / "mod.py").write_text("def target(a, b) -> int:\n    return a\n")
    (repo / "caller.py").write_text("from mod import target\n\ndef caller():\n    return target(1, 2)\n")
    head = _commit(repo, "change both")

    db = tmp_path / "g.db"
    _graph_with_card(db, fn_qname="target", fn_file="mod.py",
                     callers=[{"name": "caller", "file": "caller.py"}],
                     output_consumers=[])
    rep = contract_diff.signature_contract(str(db), str(repo), base, head)
    # caller was updated in the same diff -> downgraded to warning, not a fail
    assert all(g.get("severity") != "fail" for g in rep["gaps"])
    assert rep["ok"] is True
    assert any(g.get("severity") == "warning" for g in rep["gaps"])


def test_return_contract_change_consumer_not_in_diff_fails(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "mod.py").write_text("def producer(a) -> list:\n    return [a]\n")
    (repo / "consumer.py").write_text("from mod import producer\n\ndef eat():\n    return producer(1)\n")
    base = _commit(repo, "base")
    (repo / "mod.py").write_text("def producer(a) -> dict:\n    return {}\n")
    head = _commit(repo, "return list->dict")

    db = tmp_path / "g.db"
    _graph_with_card(db, fn_qname="producer", fn_file="mod.py",
                     callers=[],
                     output_consumers=[{"name": "eat", "file": "consumer.py"}])
    rep = contract_diff.signature_contract(str(db), str(repo), base, head)
    assert rep["ok"] is False
    assert any(g.get("severity") == "fail" for g in rep["gaps"])


def test_body_only_change_no_gaps(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "mod.py").write_text("def target(a) -> int:\n    return a\n")
    base = _commit(repo, "base")
    (repo / "mod.py").write_text("def target(a) -> int:\n    x = a\n    return x\n")
    head = _commit(repo, "body only")

    db = tmp_path / "g.db"
    _graph_with_card(db, fn_qname="target", fn_file="mod.py",
                     callers=[{"name": "caller", "file": "caller.py"}],
                     output_consumers=[])
    rep = contract_diff.signature_contract(str(db), str(repo), base, head)
    assert rep["ok"] is True
    assert rep["gaps"] == []


# ── A-2 DataFrame column-schema verdict ──────────────────────────────────────────

def _graph_with_df_card(db_path: Path, *, fn_qname: str, fn_file: str,
                        columns: dict, consumers: list[dict]):
    """Deep graph: a DataFrame-producing function + has_column->column nodes
    (per-column dtype in metadata_json) + its consistency_card output_consumers.

    columns: {col_name: dtype}; consumers: [{"name","file"}]
    """
    c = sqlite3.connect(str(db_path))
    c.executescript("""
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, line_start INTEGER,
                           line_end INTEGER, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT);
        CREATE TABLE consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
    """)
    c.execute("INSERT INTO node_type (id, name) VALUES (1,'function'),(2,'column'),(3,'dataframe')")
    c.execute("INSERT INTO edge_type (id, name) VALUES (1,'has_column')")
    short = fn_qname.split(".")[-1]
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path) "
              "VALUES (1, 1, ?, ?, ?)", (short, fn_qname, fn_file))
    nid = 2
    for col, dtype in columns.items():
        c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path, metadata_json) "
                  "VALUES (?, 2, ?, ?, ?, ?)",
                  (nid, col, col, fn_file, json.dumps({"dtype": dtype})))
        c.execute("INSERT INTO edge (edge_type_id, src_node_id, dst_node_id) VALUES (1, 1, ?)", (nid,))
        nid += 1
    for con in consumers:
        c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path) "
                  "VALUES (?, 1, ?, ?, ?)", (nid, con["name"], con["name"], con["file"]))
        nid += 1
    card = {
        "callers": [], "callees": [],
        "output_consumers": sorted({con["name"] for con in consumers}),
        "reads": [], "writes": [], "pipeline_membership": [],
        "dtype_map": {}, "columns_in": [], "columns_out": sorted(columns),
        "lineage_upstream": [], "lineage_downstream": [], "profile": [],
    }
    c.execute("INSERT INTO consistency_card (symbol_id, card_json) VALUES (1, ?)",
              (json.dumps(card),))
    c.commit()
    c.close()


def test_df_dropped_column_consumer_not_in_diff_fails(tmp_path):
    db = tmp_path / "g.db"
    _graph_with_df_card(db, fn_qname="load_df", fn_file="etl.py",
                        columns={"amount": "int64", "store_id": "int64"},
                        consumers=[{"name": "report", "file": "report.py"}])
    rep = contract_diff.dataframe_schema_contract(
        str(db), "load_df",
        base_cols={"amount": "int64", "store_id": "int64"},
        head_cols={"store_id": "int64"},          # dropped 'amount'
        changed_files={"etl.py"})                  # report.py NOT in diff
    assert rep["ok"] is False
    assert any("report" in g.get("detail", "") and g.get("severity") == "fail"
               for g in rep["gaps"])


def test_df_dropped_column_consumer_in_diff_is_warning(tmp_path):
    db = tmp_path / "g.db"
    _graph_with_df_card(db, fn_qname="load_df", fn_file="etl.py",
                        columns={"amount": "int64", "store_id": "int64"},
                        consumers=[{"name": "report", "file": "report.py"}])
    rep = contract_diff.dataframe_schema_contract(
        str(db), "load_df",
        base_cols={"amount": "int64", "store_id": "int64"},
        head_cols={"store_id": "int64"},
        changed_files={"etl.py", "report.py"})     # consumer updated
    assert rep["ok"] is True
    assert any(g.get("severity") == "warning" for g in rep["gaps"])


def test_df_retyped_column_fails(tmp_path):
    db = tmp_path / "g.db"
    _graph_with_df_card(db, fn_qname="load_df", fn_file="etl.py",
                        columns={"amount": "int64"},
                        consumers=[{"name": "report", "file": "report.py"}])
    rep = contract_diff.dataframe_schema_contract(
        str(db), "load_df",
        base_cols={"amount": "int64"},
        head_cols={"amount": "float64"},            # retyped
        changed_files={"etl.py"})
    assert rep["ok"] is False
    assert any(g.get("kind") == "column_retyped" for g in rep["gaps"])


def test_df_renamed_column_fails(tmp_path):
    db = tmp_path / "g.db"
    _graph_with_df_card(db, fn_qname="load_df", fn_file="etl.py",
                        columns={"amount": "int64"},
                        consumers=[{"name": "report", "file": "report.py"}])
    rep = contract_diff.dataframe_schema_contract(
        str(db), "load_df",
        base_cols={"amount": "int64"},
        head_cols={"amt": "int64"},                 # renamed amount->amt
        changed_files={"etl.py"})
    assert rep["ok"] is False
    # renamed manifests as a dropped 'amount' (+ a new 'amt')
    assert any("amount" in g.get("detail", "") for g in rep["gaps"])


def test_df_no_change_is_clean(tmp_path):
    db = tmp_path / "g.db"
    _graph_with_df_card(db, fn_qname="load_df", fn_file="etl.py",
                        columns={"amount": "int64"},
                        consumers=[{"name": "report", "file": "report.py"}])
    rep = contract_diff.dataframe_schema_contract(
        str(db), "load_df",
        base_cols={"amount": "int64"},
        head_cols={"amount": "int64"},
        changed_files={"etl.py"})
    assert rep["ok"] is True
    assert rep["gaps"] == []


# ── A-3 SQL query projection contract ────────────────────────────────────────────

def test_extract_sql_projection_basic():
    cols = contract_diff.extract_sql_projection(
        "SELECT store_id, amount, ts FROM sales.daily WHERE ts > '2024-01-01'")
    assert cols == {"store_id", "amount", "ts"}


def test_extract_sql_projection_aliases_and_qualified():
    cols = contract_diff.extract_sql_projection(
        "select t.store_id as sid, SUM(t.amount) AS total from sales t group by 1")
    # projected NAMES are the output aliases / final column identifiers
    assert "sid" in cols
    assert "total" in cols


def test_extract_sql_projection_star_returns_star():
    cols = contract_diff.extract_sql_projection("SELECT * FROM t")
    assert cols == {"*"}


def _graph_with_sql_reader(db_path: Path, *, table: str, reader: str,
                           reader_file: str):
    """Graph: a function --reads_sql--> sql_table, so a query projection change
    can be cross-referenced to the reader's file."""
    c = sqlite3.connect(str(db_path))
    c.executescript("""
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, line_start INTEGER,
                           line_end INTEGER, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT);
    """)
    c.execute("INSERT INTO node_type (id, name) VALUES (1,'function'),(2,'sql_table')")
    c.execute("INSERT INTO edge_type (id, name) VALUES (1,'reads_sql')")
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path) "
              "VALUES (1, 1, ?, ?, ?)", (reader, reader, reader_file))
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path) "
              "VALUES (2, 2, ?, ?, ?)", (table, table, "query.sql"))
    c.execute("INSERT INTO edge (edge_type_id, src_node_id, dst_node_id) VALUES (1, 1, 2)")
    c.commit()
    c.close()


def test_sql_contract_removed_column_reader_not_in_diff_fails(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "query.sql").write_text("SELECT store_id, amount, ts FROM sales.daily\n")
    base = _commit(repo, "base")
    (repo / "query.sql").write_text("SELECT store_id, ts FROM sales.daily\n")  # dropped amount
    head = _commit(repo, "drop amount")

    db = tmp_path / "g.db"
    _graph_with_sql_reader(db, table="sales.daily", reader="load_sales",
                           reader_file="loader.py")  # loader.py NOT in diff
    rep = contract_diff.sql_contract(str(db), str(repo), base, head)
    assert rep["ok"] is False
    assert any(g.get("severity") == "fail" and "load_sales" in g.get("detail", "")
               for g in rep["gaps"])


def test_sql_contract_added_column_is_flagged(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "query.sql").write_text("SELECT store_id FROM sales.daily\n")
    base = _commit(repo, "base")
    (repo / "query.sql").write_text("SELECT store_id, region FROM sales.daily\n")
    head = _commit(repo, "add region")

    db = tmp_path / "g.db"
    _graph_with_sql_reader(db, table="sales.daily", reader="load_sales",
                           reader_file="loader.py")
    rep = contract_diff.sql_contract(str(db), str(repo), base, head)
    # projection changed; surfaced (added cols are warnings — not a hard break)
    assert any("region" in g.get("detail", "") for g in rep["gaps"])


def test_sql_contract_scopes_readers_to_changed_table(tmp_path):
    # PSG-C6: changing one table's query must not implicate readers of OTHER tables.
    repo = _init_repo(tmp_path)
    (repo / "query.sql").write_text("SELECT store_id, amount FROM sales.daily\n")
    base = _commit(repo, "base")
    (repo / "query.sql").write_text("SELECT store_id FROM sales.daily\n")  # drop amount
    head = _commit(repo, "drop amount")

    db = tmp_path / "g.db"
    c = sqlite3.connect(str(db))
    c.executescript("""
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, line_start INTEGER,
                           line_end INTEGER, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT);
    """)
    c.execute("INSERT INTO node_type (id, name) VALUES (1,'function'),(2,'sql_table')")
    c.execute("INSERT INTO edge_type (id, name) VALUES (1,'reads_sql')")
    c.execute("INSERT INTO node (id,node_type_id,name,qualified_name,file_path) "
              "VALUES (1,1,'load_sales','load_sales','loader.py')")
    c.execute("INSERT INTO node (id,node_type_id,name,qualified_name,file_path) "
              "VALUES (2,2,'sales.daily','sales.daily','query.sql')")
    c.execute("INSERT INTO node (id,node_type_id,name,qualified_name,file_path) "
              "VALUES (3,1,'load_other','load_other','other.py')")
    c.execute("INSERT INTO node (id,node_type_id,name,qualified_name,file_path) "
              "VALUES (4,2,'other.table','other.table','other.sql')")
    c.execute("INSERT INTO edge (edge_type_id,src_node_id,dst_node_id) VALUES (1,1,2),(1,3,4)")
    c.commit()
    c.close()

    rep = contract_diff.sql_contract(str(db), str(repo), base, head)
    details = " ".join(g.get("detail", "") for g in rep["gaps"])
    assert "load_sales" in details, "the reader of the changed table must be flagged"
    assert "load_other" not in details, "a reader of an unrelated table must NOT be flagged"


def test_sql_contract_no_change_clean(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "query.sql").write_text("SELECT store_id, amount FROM sales.daily\n")
    base = _commit(repo, "base")
    (repo / "README.md").write_text("docs\n")
    head = _commit(repo, "unrelated")

    db = tmp_path / "g.db"
    _graph_with_sql_reader(db, table="sales.daily", reader="load_sales",
                           reader_file="loader.py")
    rep = contract_diff.sql_contract(str(db), str(repo), base, head)
    assert rep["ok"] is True
    assert rep["gaps"] == []


# ── PSG-C5: card/file resolution must be module-qualified, not arbitrary ──────────

def _graph_two_helpers(db_path):
    import sqlite3
    c = sqlite3.connect(str(db_path))
    c.executescript("""
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT);
        CREATE TABLE consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT);
    """)
    c.execute("INSERT INTO node_type (id, name) VALUES (1, 'function')")
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path) "
              "VALUES (1, 1, 'helper', 'pkg.a.helper', 'pkg/a.py')")
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path) "
              "VALUES (2, 1, 'helper', 'pkg.b.helper', 'pkg/b.py')")
    c.execute("INSERT INTO consistency_card (symbol_id, card_json) VALUES (1, ?)",
              ('{"callers": ["caller_a"]}',))
    c.execute("INSERT INTO consistency_card (symbol_id, card_json) VALUES (2, ?)",
              ('{"callers": ["caller_b"]}',))
    c.commit()
    return c


def test_card_for_resolves_by_module(tmp_path):
    c = _graph_two_helpers(tmp_path / "g.db")
    try:
        # module-qualified -> the RIGHT helper's card
        assert contract_diff._card_for(c, "helper", module="pkg.a")["callers"] == ["caller_a"]
        assert contract_diff._card_for(c, "helper", module="pkg.b")["callers"] == ["caller_b"]
        # ambiguous bare name (no module) -> None, never an arbitrary card
        assert contract_diff._card_for(c, "helper") is None
    finally:
        c.close()


def test_file_of_module_qualified_and_unique_only(tmp_path):
    c = _graph_two_helpers(tmp_path / "g.db")
    try:
        assert contract_diff._file_of(c, "helper", module="pkg.a") == "pkg/a.py"
        assert contract_diff._file_of(c, "helper") is None  # ambiguous -> no guess
    finally:
        c.close()
