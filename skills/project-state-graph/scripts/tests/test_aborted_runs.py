"""FL-028: a refresh killed mid-way (start_run done, finish_run never reached)
must not pass for an observation. The next start_run of the same project marks
it aborted=1; the history readers (events_of, symbol-card history, the history
CLI) ignore its events; selfcheck warns about it."""
import io
from contextlib import redirect_stdout

import selfcheck
from analyzer import cards, cli, data_model, dataflow, dataflow_types, history, py_ast, sql_refs, store, walker
from tests.test_history import BASE, _full_run, _key, conn  # noqa: F401

CHANGED = {**BASE, "pkg/m.py": BASE["pkg/m.py"].replace("df.q > 0", "df.q > 5")}


def _killed_run(conn, repo, files) -> int:
    """Everything cli.run does up to stamp_run — then the process dies."""
    for rel, src in files.items():
        (repo / rel).write_text(src)
    run_id = store.start_run(conn, project_name="demo", commit_sha="c2")
    store.reset_graph(conn)
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    data_model.analyze(conn, str(repo), fm)
    sql_refs.analyze(conn, str(repo), fm)
    history.snapshot_run(conn, str(repo), run_id)
    history.resolve(conn, run_id)
    store.stamp_run(conn, run_id)
    return run_id                                  # no finish_run


def _aborted(conn, run_id):
    return conn.execute("SELECT aborted FROM analysis_run WHERE id=?", (run_id,)).fetchone()[0]


def test_next_start_run_marks_the_unfinished_run_aborted_and_history_ignores_it(conn, tmp_path):
    r1 = _full_run(conn, tmp_path, BASE)
    key = _key(conn, r1, "pkg.m.clean")
    r2 = _killed_run(conn, tmp_path, CHANGED)
    r3 = store.start_run(conn, project_name="demo", commit_sha="c3")
    assert _aborted(conn, r2) == 1
    assert _aborted(conn, r1) in (0, None) and _aborted(conn, r3) in (0, None)
    runs = {e["run_id"] for e in history.events_of(conn, key)}
    assert r2 not in runs and r1 in runs
    # a run of ANOTHER project is never touched
    other = store.start_run(conn, project_name="other", commit_sha="x")
    store.start_run(conn, project_name="demo", commit_sha="c4")
    assert _aborted(conn, other) in (0, None)


def test_symbol_card_history_and_cli_skip_aborted_runs(conn, tmp_path):
    r1 = _full_run(conn, tmp_path, BASE)
    key = _key(conn, r1, "pkg.m.clean")
    r2 = _killed_run(conn, tmp_path, CHANGED)
    r3 = _full_run(conn, tmp_path, CHANGED)                  # marks r2 aborted, then a real run
    cards.build_symbol_cards(conn)
    cards.attach_history(conn)
    import json
    card = json.loads(conn.execute(
        "SELECT sc.card_json FROM symbol_card sc JOIN node n ON n.id=sc.symbol_id WHERE n.qualified_name='pkg.m.clean'"
    ).fetchone()[0])
    assert all(h["run_id"] != r2 for h in card["history"]) and any(h["run_id"] == r3 for h in card["history"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.history_main([str(tmp_path / "demo-state-graph.db"), key])
    out = buf.getvalue()
    assert f"run={r3} " in out and f"run={r2} " not in out


def test_selfcheck_warns_about_aborted_runs(conn, tmp_path):
    _full_run(conn, tmp_path, BASE)
    before = selfcheck.run(str(tmp_path / "demo-state-graph.db"))
    chk = next(c for c in before["checks"] if c["name"] == "aborted_runs")
    assert chk["ok"] is True and chk["severity"] == "warning"
    r2 = _killed_run(conn, tmp_path, CHANGED)
    _full_run(conn, tmp_path, CHANGED)
    res = selfcheck.run(str(tmp_path / "demo-state-graph.db"))
    chk = next(c for c in res["checks"] if c["name"] == "aborted_runs")
    assert chk["ok"] is False and chk["severity"] == "warning"
    assert "1 aborted" in chk["detail"] and f"run {r2}" in chk["detail"]
    assert res["ok"] == before["ok"]                          # a warning never flips the verdict
