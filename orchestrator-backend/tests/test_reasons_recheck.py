"""`provledger reasons recheck` — find, and with --apply correct, the rows the
close-time rules wrote before FL-238 / FL-239 (2026-10-08).

Two kinds of row are wrong and stay in the ledger (append-only):
  * a `stated` reason quoting text Claude Code injected (a subagent's report, a
    reminder): nobody said it. The correction is an `unstated` row by the
    system for the same plan and node, which supersedes it.
  * an R6 rejected path hung on a node its text never names as code. The
    correction is the same text, anchored by today's rule (a node, or the
    plan), which supersedes it.
Without --apply nothing is written. A second --apply finds nothing."""
import json
import sys
from pathlib import Path

import pytest

from orchestrator import api, cli, provenance as pv, reasons, triggers

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

PLAN = "P1"
REPO = Path(__file__).resolve().parents[2]
HANDBACK = ('<agent-message from="a1b2c3">\n[Subagent hand-back] The text below is the final report of a subagent.\n'
            "Keep snapshot() on fiscal weeks.\n</agent-message>")
USER = "please keep Driver.run as it is"


@pytest.fixture
def graph(tmp_path):
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_run(c, 2, plan_id=PLAN, step_id=f"{PLAN}-REVIEW.1")
    for i, (k, qn) in enumerate((("nk_run", "skills.review_run.Driver.run"), ("nk_snap", "scripts.home_guard.snapshot")), 1):
        for run, sig in ((1, "s1"), (2, "s2")):
            c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, struct_sig, "
                      "dataflow_sig, dataflow_trivial, attrs_json) VALUES (?, ?, 'function', ?, 'pkg/m.py', ?, NULL, 1, '{}')",
                      (run, k, qn, sig))
        ps.add_event(c, 1, i, "node_added", k)
        ps.add_event(c, 2, i, "node_changed", k, '{"changed": ["struct_sig"]}')
    c.commit()
    c.close()
    return str(path)


@pytest.fixture
def ledger(conn, graph, monkeypatch):
    """What an older release left: a stated row quoting a hand-back, a stated row
    quoting the user, an R6 row hung on `run` for a text that names no code, and
    an R6 row on the node its text names."""
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) VALUES "
                 "(?, 'goal', 'IN_PROGRESS', 'proj', 'declared', '2026-09-15 10:00:00')", (PLAN,))
    for i, s in enumerate(("A", "B")):
        conn.execute("INSERT INTO Steps (step_id, plan_id, description, status, execution_order, depth_level, log_context, step_type) "
                     "VALUES (?, ?, ?, 'PENDING', ?, 0, '', 'COMMAND')", (f"{PLAN}-{s}", PLAN, f"step {s}", i))
    conn.commit()
    for s, why in (("A", "run the suites; the ledger stayed locked"), ("B", "snapshot() crashed on a NULL row")):
        api.start_step(conn, f"{PLAN}-{s}")
        api.fail_step(conn, f"{PLAN}-{s}", reason=why)
    ctx = triggers._ctx(conn, "proj", PLAN, graph)
    texts = {step: text for text, step, _ in triggers.r6_candidates(ctx)}
    old_r6 = pv.insert_reason(conn, project="proj", plan_id=PLAN, node_key="nk_run", kind="technical", role="rejected_path",
                              interpretation=texts[f"{PLAN}-A"], step_id=f"{PLAN}-A", rule_id="R6", recorded_by="system")
    good_r6 = pv.insert_reason(conn, project="proj", plan_id=PLAN, node_key="nk_snap", kind="technical", role="rejected_path",
                               interpretation=texts[f"{PLAN}-B"], step_id=f"{PLAN}-B", rule_id="R6", recorded_by="system")
    hb = pv.insert_utterance(conn, session_id="s7", project="proj", plan_id=PLAN, text=HANDBACK, occurred_at="2026-09-15 10:05:00")
    us = pv.insert_utterance(conn, session_id="s7", project="proj", plan_id=PLAN, text=USER, occurred_at="2026-09-15 10:06:00")
    monkeypatch.setattr(pv, "is_injected_prompt", lambda text: False)      # the store had no gate then
    false = pv.insert_reason(conn, project="proj", plan_id=PLAN, node_key="nk_snap", kind="technical",
                             verbatim=(hb, 0, 40), rule_id="R0", recorded_by="system")
    monkeypatch.undo()
    true = pv.insert_reason(conn, project="proj", plan_id=PLAN, node_key="nk_run", kind="technical",
                            verbatim=(us, 0, len(USER)), rule_id="R0", recorded_by="system")
    reg = Path(graph).parent / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "proj", "repo": str(Path(graph).parent), "db_path": graph}]}))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    return {"false": false, "true": true, "old_r6": old_r6, "good_r6": good_r6}


def _rows(conn):
    return [tuple(r) for r in conn.execute("SELECT id, superseded_by FROM change_reason ORDER BY id")]


def test_a_dry_run_names_the_two_wrong_rows_and_writes_nothing(conn, ledger):
    before = _rows(conn)
    out = reasons.recheck(conn, project="proj", apply=False)
    assert [r["id"] for r in out["stated"]] == [ledger["false"]]
    assert [(r["id"], r["from"], r["to"]) for r in out["rejected"]] == [(ledger["old_r6"], "nk_run", None)]
    assert out["written"] == 0 and _rows(conn) == before


def test_apply_appends_a_correction_that_supersedes_each_wrong_row(conn, ledger):
    out = reasons.recheck(conn, project="proj", apply=True)
    assert out["written"] == 2
    fix = pv.get_reason(conn, pv.get_reason(conn, ledger["false"])["superseded_by"])
    assert (fix["tier"], fix["recorded_by"], fix["plan_id"], fix["node_key"]) == ("unstated", "system", PLAN, "nk_snap")
    moved = pv.get_reason(conn, pv.get_reason(conn, ledger["old_r6"])["superseded_by"])
    old = pv.get_reason(conn, ledger["old_r6"])
    assert (moved["role"], moved["rule_id"], moved["node_key"]) == ("rejected_path", "R6", None)
    assert moved["interpretation"] == old["interpretation"] and moved["step_id"] == old["step_id"]
    assert pv.get_reason(conn, ledger["true"])["superseded_by"] is None
    assert pv.get_reason(conn, ledger["good_r6"])["superseded_by"] is None
    again = reasons.recheck(conn, project="proj", apply=True)
    assert (again["stated"], again["rejected"], again["written"]) == ([], [], 0)


def test_the_cli_is_a_dry_run_unless_told_to_apply(conn, ledger, tmp_path, monkeypatch, capsys):
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("ORCH_DB", dbp)
    assert cli.main(["reasons", "recheck", "--project", "proj"]) == 0
    text = capsys.readouterr().out
    assert f"#{ledger['false']}" in text and f"#{ledger['old_r6']}" in text and "--apply" in text
    assert pv.get_reason(conn, ledger["false"])["superseded_by"] is None
    assert cli.main(["reasons", "recheck", "--project", "proj", "--apply"]) == 0
    assert "2 correction(s) written" in capsys.readouterr().out


def test_the_command_is_in_the_reference_table():
    assert "`reasons recheck`" in (REPO / "docs" / "cli.md").read_text()
