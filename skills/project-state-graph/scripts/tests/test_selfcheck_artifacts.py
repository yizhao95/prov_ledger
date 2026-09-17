"""selfcheck: how much of this project's arithmetic nobody can trace, and how
many anchors are lost (DP phase 4, Task 2).

Two numbers, both informational, neither of which flips `ok`.

`manual_figures` is the share of metric-like nodes that are hand-computed. It
is not a failure — a hand-computed figure that says so is honest — but a
project where most of the numbers in the deck came from somebody's head is a
different project from one where they came from a pipeline, and the only way
anybody notices is if the ratio is printed.

`anchor_lost` is the count of anchors that were lost the last time anyone
looked. An anchor_lost never blocks anything (spec §9: it lowers what the
ledger can claim, it does not break the ledger), and a failure that never
blocks has nowhere to be seen unless it is counted here.
"""
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from analyzer import cli  # noqa: E402
import selfcheck  # noqa: E402

from .test_session_trigger import _repo  # noqa: E402


def _orch(tmp_path, *, metrics=(), figures=(), occurrences=()):
    """An orchestrator DB with just the four tables these two checks read."""
    orch = tmp_path / "orch.db"
    c = sqlite3.connect(str(orch))
    c.executescript("""
        CREATE TABLE metrics (id INTEGER PRIMARY KEY, project TEXT, name TEXT, value REAL);
        CREATE TABLE declared_node (id INTEGER PRIMARY KEY, project TEXT, qualified_name TEXT, node_type TEXT,
                                    state TEXT, superseded_by INTEGER);
        CREATE TABLE occurrence (id INTEGER PRIMARY KEY, project TEXT, node_key TEXT);
        CREATE TABLE anchor_state (id INTEGER PRIMARY KEY, occurrence_id INTEGER, state TEXT, checked_at TEXT);
    """)
    for project, name in metrics:
        c.execute("INSERT INTO metrics (project, name, value) VALUES (?, ?, 1.0)", (project, name))
    for project, qn in figures:
        c.execute("INSERT INTO declared_node (project, qualified_name, node_type, state, superseded_by) "
                  "VALUES (?, ?, 'manual_figure', 'active', NULL)", (project, qn))
    for i, (project, node_key, state) in enumerate(occurrences, start=1):
        c.execute("INSERT INTO occurrence (id, project, node_key) VALUES (?, ?, ?)", (i, project, node_key))
        if state:
            c.execute("INSERT INTO anchor_state (occurrence_id, state, checked_at) VALUES (?, 'ok', '2026-09-17 09:00:00')", (i,))
            c.execute("INSERT INTO anchor_state (occurrence_id, state, checked_at) VALUES (?, ?, '2026-09-17 10:00:00')", (i, state))
    c.commit()
    c.close()
    return orch


def _graph(tmp_path):
    repo = _repo(tmp_path)
    dbp = tmp_path / "g.db"
    cli.main([str(repo), "--project", "proj", "--db-path", str(dbp)])
    return dbp


def _check(dbp, name):
    return next(x for x in selfcheck.run(str(dbp))["checks"] if x["name"] == name)


def test_selfcheck_prints_the_share_of_figures_nobody_can_trace(tmp_path, monkeypatch):
    dbp = _graph(tmp_path)
    orch = _orch(tmp_path,
                 metrics=[("demo", "q3_conv"), ("demo", "net_revenue"), ("demo", "q3_conv")],
                 figures=[("demo", "declared:q3-conv-manual")],
                 occurrences=[("demo", "metric:q3_conv", "ok")])
    monkeypatch.setenv("ORCH_DB", str(orch))
    chk = _check(dbp, "manual_figures")
    assert chk["severity"] == "warning" and chk["ok"] is True      # informational, never flips ok
    # metric-like nodes: metric:q3_conv, metric:net_revenue, declared:q3-conv-manual — the repeated
    # metric row is one node, and the occurrence points at a node that is already counted
    assert chk["metric_like"] == 3 and chk["count"] == 1
    assert chk["ratio"] == 0.3333
    assert "1 of 3" in chk["detail"] and "no traceable data source" in chk["detail"]


def test_selfcheck_counts_the_anchors_that_were_lost_the_last_time_anyone_looked(tmp_path, monkeypatch):
    dbp = _graph(tmp_path)
    orch = _orch(tmp_path,
                 metrics=[("demo", "q3_conv")],
                 occurrences=[("demo", "metric:q3_conv", "anchor_lost"),
                              ("demo", "metric:q3_conv", "ok"),
                              ("demo", "metric:q3_conv", None)])
    monkeypatch.setenv("ORCH_DB", str(orch))
    chk = _check(dbp, "anchor_lost")
    assert chk["severity"] == "warning" and chk["ok"] is True
    assert chk["count"] == 1 and chk["anchors"] == 3 and chk["unchecked"] == 1
    assert "1 of 3" in chk["detail"]


def test_both_checks_say_zero_when_there_is_no_orchestrator_db(tmp_path, monkeypatch):
    dbp = _graph(tmp_path)
    monkeypatch.setenv("ORCH_DB", str(tmp_path / "missing.db"))
    for name in ("manual_figures", "anchor_lost"):
        chk = _check(dbp, name)
        assert chk["ok"] is True and "0" in chk["detail"]


def test_a_project_with_no_figures_at_all_reports_zero_rather_than_dividing_by_zero(tmp_path, monkeypatch):
    dbp = _graph(tmp_path)
    monkeypatch.setenv("ORCH_DB", str(_orch(tmp_path)))
    chk = _check(dbp, "manual_figures")
    assert chk["ok"] is True and chk["metric_like"] == 0 and chk["ratio"] == 0.0
