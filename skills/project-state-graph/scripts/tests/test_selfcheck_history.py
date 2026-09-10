"""selfcheck history gates (spec §2.7): two WARN, two ERROR."""
import json

import selfcheck
from analyzer import cards, dataflow, dataflow_types, history, py_ast, store, walker


def _run(conn, repo, src, plan_id=None):
    (repo / "pkg").mkdir(parents=True, exist_ok=True)
    (repo / "pkg" / "m.py").write_text(src)
    run_id = store.start_run(conn, project_name="demo", commit_sha="deadbeef", plan_id=plan_id)
    store.reset_graph(conn)
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    cards.build_symbol_cards(conn)
    history.snapshot_run(conn, str(repo), run_id)
    history.resolve(conn, run_id)
    cards.attach_history(conn)
    store.stamp_run(conn, run_id)
    store.finish_run(conn, run_id)
    return run_id


SRC = "def helper():\n    return [1, 2]\n\ndef index():\n    rows = helper()\n    return rows\n"


def _checks(path):
    res = selfcheck.run(path)
    return res, {c["name"]: c for c in res["checks"]}


def test_history_gates_pass_on_good_db(tmp_path):
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    _run(conn, tmp_path / "repo", SRC)
    _run(conn, tmp_path / "repo", SRC)
    conn.close()
    res, by = _checks(path)
    assert res["ok"], res["report"]
    for name, sev in (("history_ambiguous", "warning"), ("history_broken_ratio", "warning"),
                      ("history_append_only", "error"), ("history_key_coverage", "error")):
        assert by[name]["ok"] is True and by[name]["severity"] == sev, by[name]


def test_history_append_only_fails_when_a_trigger_is_missing(tmp_path):
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    _run(conn, tmp_path / "repo", SRC)
    conn.execute("DROP TRIGGER trg_node_event_no_delete"); conn.commit(); conn.close()
    res, by = _checks(path)
    assert res["ok"] is False and by["history_append_only"]["ok"] is False
    assert "trg_node_event_no_delete" in by["history_append_only"]["detail"]


def test_history_key_coverage_fails_on_unkeyed_symbol(tmp_path):
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    _run(conn, tmp_path / "repo", SRC)
    conn.execute("UPDATE node SET node_key=NULL WHERE qualified_name='pkg.m.helper'"); conn.commit(); conn.close()
    res, by = _checks(path)
    assert res["ok"] is False and by["history_key_coverage"]["ok"] is False
    assert "pkg.m.helper" in by["history_key_coverage"]["detail"]


def test_history_ambiguous_and_broken_are_warnings(tmp_path):
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    _run(conn, tmp_path / "repo", "def norm_a(x):\n    return x + 1\n\ndef norm_b(x):\n    return x + 1\n\ndef gone():\n    return 0\n")
    r2 = _run(conn, tmp_path / "repo", "def scale_a(x):\n    return x + 1\n\ndef scale_b(x):\n    return x + 1\n")
    conn.close()
    res, by = _checks(path)
    assert res["ok"] is True                      # warnings never flip ok
    assert by["history_ambiguous"]["ok"] is False and "1" in by["history_ambiguous"]["detail"]
    assert by["history_broken_ratio"]["ok"] is False and "1/3" in by["history_broken_ratio"]["detail"]


def test_history_gates_on_db_without_history_rows(tmp_path):
    """A DB whose latest run never snapshotted (pre-history build) must not
    crash the checks; key coverage reports the gap as an error."""
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    repo = tmp_path / "repo"; (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text(SRC)
    run_id = store.start_run(conn, project_name="demo", commit_sha="deadbeef")
    fm = walker.walk(conn, str(repo)); py_ast.analyze(conn, str(repo), fm)
    cards.build_symbol_cards(conn); store.stamp_run(conn, run_id); store.finish_run(conn, run_id); conn.close()
    res, by = _checks(path)
    assert by["history_key_coverage"]["ok"] is False
    assert by["history_ambiguous"]["ok"] is True and by["history_broken_ratio"]["ok"] is True
