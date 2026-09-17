"""selfcheck: the two rates the external-artifact judge must be measured by
(DP phase 5, Task 1; spec §4, C5).

An LLM verdict's problem is not that it is sometimes wrong. It is that when it
is wrong nobody finds out. `trigger_log` records every verdict the judge
reached; these two numbers are what the record is for:

  external_false_ask_rate  of the asks a person answered, the share they had
                           nothing to say to — the question should not have
                           been asked
  external_miss_rate       of the silences, the share the person came back to
                           of their own accord — the change was odd after all

Both are informational and neither flips `ok`: a judge that is asking too much
is a judge to recalibrate, not a build to stop. Both print their denominator,
because a rate over two answers is not the same statement as a rate over two
hundred.
"""
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from analyzer import cli  # noqa: E402
import selfcheck  # noqa: E402

from .test_session_trigger import _repo  # noqa: E402


def _orch(tmp_path, *, verdicts=(), reasons=()):
    """An orchestrator DB with just the two tables this check reads.

    verdicts: (project, plan_id, node_key, verdict)
    reasons:  (project, plan_id, node_key, tier, recorded_by)
    """
    orch = tmp_path / "orch.db"
    c = sqlite3.connect(str(orch))
    c.executescript("""
        CREATE TABLE trigger_log (id INTEGER PRIMARY KEY, project TEXT, plan_id TEXT, node_key TEXT,
                                  path TEXT, rule_id TEXT, verdict TEXT, basis TEXT, user_action TEXT, at TEXT);
        CREATE TABLE change_reason (id INTEGER PRIMARY KEY, project TEXT, plan_id TEXT, node_key TEXT,
                                    role TEXT, tier TEXT, recorded_by TEXT, state TEXT, superseded_by INTEGER);
    """)
    for project, plan_id, node_key, verdict in verdicts:
        c.execute("INSERT INTO trigger_log (project, plan_id, node_key, path, verdict, basis, at) "
                  "VALUES (?, ?, ?, 'external', ?, 'stub', '2026-09-17 10:00:00')",
                  (project, plan_id, node_key, verdict))
    for project, plan_id, node_key, tier, by in reasons:
        c.execute("INSERT INTO change_reason (project, plan_id, node_key, role, tier, recorded_by, state, superseded_by) "
                  "VALUES (?, ?, ?, 'reason', ?, ?, 'active', NULL)", (project, plan_id, node_key, tier, by))
    c.commit()
    c.close()
    return orch


def _graph(tmp_path):
    repo = _repo(tmp_path)
    dbp = tmp_path / "g.db"
    cli.main([str(repo), "--project", "proj", "--db-path", str(dbp)])
    return dbp


def _check(dbp):
    return next(x for x in selfcheck.run(str(dbp))["checks"] if x["name"] == "external_trigger_rates")


def test_selfcheck_prints_the_share_of_asks_the_person_had_nothing_to_say_to(tmp_path, monkeypatch):
    dbp = _graph(tmp_path)
    orch = _orch(tmp_path,
                 verdicts=[("demo", "P1", "metric:a", "ask"),
                           ("demo", "P1", "metric:b", "ask"),
                           ("demo", "P1", "metric:c", "ask")],
                 reasons=[("demo", "P1", "metric:a", "unstated", "human"),      # asked, nothing to say
                          ("demo", "P1", "metric:b", "stated", "human")])       # asked, worth asking
    monkeypatch.setenv("ORCH_DB", str(orch))
    chk = _check(dbp)
    assert chk["severity"] == "warning" and chk["ok"] is True
    assert chk["asks"] == 3 and chk["asks_answered"] == 2 and chk["false_asks"] == 1
    assert chk["external_false_ask_rate"] == 0.5
    assert "1 of 2" in chk["detail"]


def test_selfcheck_counts_the_silences_the_person_came_back_to(tmp_path, monkeypatch):
    dbp = _graph(tmp_path)
    orch = _orch(tmp_path,
                 verdicts=[("demo", "P1", "metric:a", "silent"),
                           ("demo", "P1", "metric:b", "silent"),
                           ("demo", "P1", "metric:c", "silent"),
                           ("demo", "P1", "metric:d", "silent")],
                 reasons=[("demo", "P1", "metric:a", "stated", "human"),        # a note nobody asked for
                          ("demo", "P1", "metric:b", "derived", "system")])     # the system's own row is not a miss
    monkeypatch.setenv("ORCH_DB", str(orch))
    chk = _check(dbp)
    assert chk["silences"] == 4 and chk["misses"] == 1
    assert chk["external_miss_rate"] == 0.25
    assert "1 of 4" in chk["detail"]


def test_a_rate_with_no_answers_behind_it_is_reported_as_such_not_as_zero(tmp_path, monkeypatch):
    dbp = _graph(tmp_path)
    monkeypatch.setenv("ORCH_DB", str(_orch(tmp_path)))
    chk = _check(dbp)
    assert chk["ok"] is True
    assert chk["asks"] == 0 and chk["silences"] == 0
    assert chk["external_false_ask_rate"] is None and chk["external_miss_rate"] is None
    assert "0 of 0" in chk["detail"]


def test_the_rates_say_zero_when_there_is_no_orchestrator_db(tmp_path, monkeypatch):
    dbp = _graph(tmp_path)
    monkeypatch.setenv("ORCH_DB", str(tmp_path / "missing.db"))
    chk = _check(dbp)
    assert chk["ok"] is True and "0" in chk["detail"]
