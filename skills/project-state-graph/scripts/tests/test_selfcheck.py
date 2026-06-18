"""RED tests for selfcheck — graph invariants on a built DB."""
import pytest

import selfcheck
from analyzer import (
    store, walker, py_ast, dataflow, dataflow_types,
    sql_refs, pipeline, cards, cli,
)


def _build_good_db(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text(
        "def helper():\n    return [1, 2]\n\n"
        "def index():\n    rows = helper()\n    return rows\n"
    )
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    run_id = store.start_run(conn, project_name="demo", commit_sha="deadbeef")
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    sql_refs.analyze(conn, str(repo), fm)
    pipeline.analyze(conn, str(repo), fm)
    cards.build_symbol_cards(conn)
    store.finish_run(conn, run_id)
    conn.commit()
    return path, conn


def test_selfcheck_passes_on_good_db(tmp_path):
    path, conn = _build_good_db(tmp_path)
    conn.close()
    result = selfcheck.run(path)
    assert result["ok"] is True, result
    # each named invariant present and passing
    names = {c["name"]: c["ok"] for c in result["checks"]}
    assert names["no_dangling_edges"] is True
    assert names["cards_match_callables"] is True
    assert names["commit_sha_set"] is True
    assert names["node_types_nonempty"] is True


def test_selfcheck_fails_on_dangling_edge(tmp_path):
    path, conn = _build_good_db(tmp_path)
    # inject a dangling edge: dst_node_id pointing at a non-existent node
    et = store.get_or_create_edge_type(conn, "calls")
    some_src = conn.execute("SELECT id FROM node LIMIT 1").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")  # simulate corruption that bypassed FK
    conn.execute(
        "INSERT INTO edge (edge_type_id, src_node_id, dst_node_id, metadata_json) "
        "VALUES (?, ?, ?, ?)",
        (et, some_src, 9999999, "{}"),
    )
    conn.commit()
    conn.close()
    result = selfcheck.run(path)
    assert result["ok"] is False
    dangling = next(c for c in result["checks"] if c["name"] == "no_dangling_edges")
    assert dangling["ok"] is False


def test_selfcheck_report_is_human_readable(tmp_path):
    path, conn = _build_good_db(tmp_path)
    conn.close()
    result = selfcheck.run(path)
    assert isinstance(result["report"], str)
    assert "no_dangling_edges" in result["report"]


# --- severity tiers + new checks ---

def test_every_check_has_severity(tmp_path):
    path, conn = _build_good_db(tmp_path)
    conn.close()
    result = selfcheck.run(path)
    for c in result["checks"]:
        assert c["severity"] in ("error", "warning"), c


def test_no_undefined_symbols_check_passes_on_good_db(tmp_path):
    path, conn = _build_good_db(tmp_path)
    conn.close()
    result = selfcheck.run(path)
    names = {c["name"]: c for c in result["checks"]}
    assert "no_undefined_symbols" in names
    assert names["no_undefined_symbols"]["severity"] == "error"
    assert names["no_undefined_symbols"]["ok"] is True


def test_undefined_symbol_hard_fails(tmp_path):
    path, conn = _build_good_db(tmp_path)
    # inject an unresolved_call node (what the unresolved pass would record)
    ut = store.get_or_create_node_type(conn, "unresolved_call")
    store.add_node(conn, ut, name="bulid_query", qualified_name="bulid_query",
                   file_path="pkg/m.py", line_start=2, metadata={"name": "bulid_query"})
    conn.commit()
    conn.close()
    result = selfcheck.run(path)
    assert result["ok"] is False  # error severity flips overall ok
    undef = next(c for c in result["checks"] if c["name"] == "no_undefined_symbols")
    assert undef["ok"] is False
    assert undef["severity"] == "error"


def test_isolated_node_is_warning_not_blocker(tmp_path):
    path, conn = _build_good_db(tmp_path)
    # inject an isolated function node: a callable with NO edges at all
    ft = store.get_or_create_node_type(conn, "function")
    store.add_node(conn, ft, name="orphan", qualified_name="pkg.m.orphan",
                   file_path="pkg/m.py", line_start=99)
    # rebuild cards so cards_match_callables (an error check) stays satisfied;
    # we want ONLY the isolation warning to trip.
    cards.build_symbol_cards(conn)
    conn.commit()
    conn.close()
    result = selfcheck.run(path)
    iso = next(c for c in result["checks"] if c["name"] == "no_isolated_nodes")
    assert iso["severity"] == "warning"
    assert iso["ok"] is False               # the warning DID trip
    assert result["ok"] is True             # but it does NOT block
    assert "WARN" in result["report"]


# --- v2 data-aware invariants ---

def test_new_data_invariants_present_with_severity(tmp_path):
    path, conn = _build_good_db(tmp_path)
    conn.close()
    result = selfcheck.run(path)
    names = {c["name"]: c for c in result["checks"]}
    assert names["dtype_present"]["severity"] == "warning"
    assert names["dtype_consistency_e2e"]["severity"] == "error"
    assert names["lineage_no_dangling"]["severity"] == "error"
    assert names["profile_assigned"]["severity"] == "warning"


def test_dtype_present_is_warning_not_blocker(tmp_path):
    path, conn = _build_good_db(tmp_path)
    # inject an untyped/unknown column
    ct = store.get_or_create_node_type(conn, "column")
    store.add_node(conn, ct, name="mystery", qualified_name="mystery",
                   file_path="pkg/m.py",
                   metadata={"dtype": "unknown", "dtype_provenance": "unknown"})
    conn.commit()
    conn.close()
    result = selfcheck.run(path)
    chk = next(c for c in result["checks"] if c["name"] == "dtype_present")
    assert chk["ok"] is False        # it tripped
    assert result["ok"] is True      # but it does NOT block (warning)


def test_dtype_consistency_e2e_hard_fails_on_mismatch(tmp_path):
    path, conn = _build_good_db(tmp_path)
    # build a produces->consumes chain with mismatched dtypes
    dv_t = store.get_or_create_node_type(conn, "data_var")
    fn_t = store.get_or_create_node_type(conn, "function")
    produces_e = store.get_or_create_edge_type(conn, "produces")
    consumes_e = store.get_or_create_edge_type(conn, "consumes")
    prod = store.add_node(conn, fn_t, name="prod", qualified_name="prod", file_path="pkg/m.py")
    cons = store.add_node(conn, fn_t, name="cons", qualified_name="cons", file_path="pkg/m.py")
    dv = store.add_node(conn, dv_t, name="prod:return", qualified_name="prod:return",
                        file_path="pkg/m.py", metadata={"dtype": "int"})
    store.add_edge(conn, produces_e, prod, dv, metadata={"type": "int"})
    store.add_edge(conn, consumes_e, dv, cons, metadata={"type": "str"})
    cards.build_symbol_cards(conn)
    conn.commit()
    conn.close()
    result = selfcheck.run(path)
    chk = next(c for c in result["checks"] if c["name"] == "dtype_consistency_e2e")
    assert chk["ok"] is False
    assert chk["severity"] == "error"
    assert result["ok"] is False     # blocks


def test_lineage_no_dangling_passes_on_good_db(tmp_path):
    path, conn = _build_good_db(tmp_path)
    conn.close()
    result = selfcheck.run(path)
    chk = next(c for c in result["checks"] if c["name"] == "lineage_no_dangling")
    assert chk["ok"] is True
