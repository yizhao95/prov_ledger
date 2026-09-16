"""R0 sees the words that caused the plan (DP phase 2b, Task 0; FL-069 / FL-066):
a plan carries its session, so what was said in that session before the plan
existed is a candidate; a file name anchors only to the nodes changed in that
file this time, without the "more than 3 nodes" limit."""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import db, provenance as pv, psg_bridge, triggers

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

PLAN = "P1"
NODES = {"nk_a": ("pkg.rollup.weekly_totals", "pkg/rollup.py"), "nk_b": ("pkg.m.run", "pkg/m.py"),
         "nk_c": ("pkg.m.clean_rows", "pkg/m.py"), "nk_d": ("pkg.m.load_orders", "pkg/m.py"),
         "nk_e": ("pkg.m.save_all", "pkg/m.py"), "nk_f": ("pkg.m.untouched", "pkg/m.py")}


def _graph(tmp_path, changed=("nk_a", "nk_b", "nk_c", "nk_d", "nk_e")):
    """All six nodes exist since run 1; `changed` get a struct_sig change in run 2 (the plan's run); nk_f never changes."""
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0"); ps.add_run(c, 2, plan_id=PLAN, step_id=f"{PLAN}-REVIEW.1")
    for i, (k, (qn, fp)) in enumerate(NODES.items(), 1):
        c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, struct_sig, dataflow_sig, dataflow_trivial, attrs_json) VALUES (1, ?, 'function', ?, ?, 's1', NULL, 1, '{}')", (k, qn, fp))
        ps.add_event(c, 1, i, "node_added", k)
        if k in changed:
            c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, struct_sig, dataflow_sig, dataflow_trivial, attrs_json) VALUES (2, ?, 'function', ?, ?, 's2', NULL, 1, '{}')", (k, qn, fp))
            ps.add_event(c, 2, i, "node_changed", k, '{"changed": ["struct_sig"]}')
        else:
            c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, struct_sig, dataflow_sig, dataflow_trivial, attrs_json) VALUES (2, ?, 'function', ?, ?, 's1', NULL, 1, '{}')", (k, qn, fp))
            ps.add_event(c, 2, i, "node_matched", k)
    c.commit(); c.close()
    return str(path)


def _plan(conn, session_id=None, created="2026-09-16 10:00:00"):
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at, session_id) VALUES (?, 'g', 'IN_PROGRESS', 'proj', 'declared', ?, ?)",
                 (PLAN, created, session_id))
    conn.commit()


def test_words_said_in_the_plans_session_before_the_plan_existed_are_candidates(conn, tmp_path):
    g = _graph(tmp_path)
    _plan(conn, session_id="sess-1")
    u = pv.insert_utterance(conn, session_id="sess-1", project="proj", plan_id=None, text="please make weekly_totals sum paid orders only.",
                            occurred_at="2026-09-16 09:55:00")                       # 5 min BEFORE the plan, no plan_id
    out = triggers.evaluate(conn, project="proj", plan_id=PLAN, psg_db_path=g, commit=True)
    assert out["by_rule"]["R0"] >= 1
    row = conn.execute("SELECT verbatim_utterance_id, verbatim_start, verbatim_end, tier, rule_id FROM change_reason WHERE plan_id=? AND node_key='nk_a'", (PLAN,)).fetchone()
    assert row[0] == u and row[3] == "stated" and row[4] == "R0"
    assert "weekly_totals" in conn.execute("SELECT substr(text, ?+1, ?-?) FROM utterance WHERE id=?", (row[1], row[2], row[1], u)).fetchone()[0]


def test_words_from_another_session_outside_the_window_are_not_candidates(conn, tmp_path):
    g = _graph(tmp_path)
    _plan(conn, session_id="sess-1")
    pv.insert_utterance(conn, session_id="sess-OTHER", project="proj", plan_id=None, text="please make weekly_totals sum paid orders only.",
                        occurred_at="2026-09-16 09:55:00")
    out = triggers.evaluate(conn, project="proj", plan_id=PLAN, psg_db_path=g, commit=True)
    assert out["by_rule"]["R0"] == 0
    assert conn.execute("SELECT COUNT(*) FROM change_reason WHERE plan_id=? AND rule_id='R0'", (PLAN,)).fetchone()[0] == 0


def test_a_file_name_anchors_to_every_node_changed_in_that_file_and_not_to_the_untouched_one(conn, tmp_path):
    g = _graph(tmp_path)                                                    # pkg/m.py: nk_b nk_c nk_d nk_e changed, nk_f untouched
    _plan(conn, session_id="sess-1")
    u = pv.insert_utterance(conn, session_id="sess-1", project="proj", plan_id=None,
                            text="Rework m.py so every loader keeps the paid filter.", occurred_at="2026-09-16 09:59:00")
    out = triggers.evaluate(conn, project="proj", plan_id=PLAN, psg_db_path=g, commit=True)
    rows = {r[0]: r for r in conn.execute("SELECT node_key, rule_id, tier, verbatim_utterance_id FROM change_reason WHERE plan_id=? AND rule_id='R0'", (PLAN,))}
    assert set(rows) == {"nk_b", "nk_c", "nk_d", "nk_e"}                    # four changed nodes in the file — no "> 3 nodes" limit here
    assert all(r[2] == "stated" and r[3] == u for r in rows.values())
    logs = conn.execute("SELECT node_key, verdict, basis FROM trigger_log WHERE plan_id=? AND rule_id='R0'", (PLAN,)).fetchall()
    assert all(v == "auto" for _, v, _ in logs) and all("name the file m.py" in b for _, _, b in logs)
    assert "nk_f" not in rows and not any(k == "nk_f" for k, _, _ in logs)   # untouched → not anchored, not ambiguous


def test_local_name_rule_is_unchanged_a_generic_sentence_stays_ambiguous(conn, tmp_path):
    g = _graph(tmp_path)
    _plan(conn, session_id="sess-1")
    pv.insert_utterance(conn, session_id="sess-1", project="proj", plan_id=None,
                        text="touch weekly_totals, clean_rows, load_orders and save_all together", occurred_at="2026-09-16 09:59:00")
    out = triggers.evaluate(conn, project="proj", plan_id=PLAN, psg_db_path=g, commit=True)
    assert out["by_rule"]["R0"] == 0
    amb = conn.execute("SELECT COUNT(*) FROM trigger_log WHERE plan_id=? AND rule_id='R0' AND verdict='ambiguous'", (PLAN,)).fetchone()[0]
    assert amb == 4 and "names 4 nodes" in conn.execute("SELECT basis FROM trigger_log WHERE plan_id=? AND verdict='ambiguous' LIMIT 1", (PLAN,)).fetchone()[0]


def test_changed_in_file_lists_only_this_plans_changes(tmp_path):
    g = _graph(tmp_path)
    assert sorted(n["node_key"] for n in psg_bridge.changed_in_file(g, PLAN, "pkg/m.py")) == ["nk_b", "nk_c", "nk_d", "nk_e"]
    assert [n["node_key"] for n in psg_bridge.changed_in_file(g, PLAN, "m.py")] == ["nk_b", "nk_c", "nk_d", "nk_e"]      # basename form
    assert psg_bridge.changed_in_file(g, PLAN, "pkg/rollup.py") == [{"node_key": "nk_a", "qualified_name": "pkg.rollup.weekly_totals", "file_path": "pkg/rollup.py"}]
    assert psg_bridge.changed_in_file(g, "P-no-run", "pkg/m.py") == []                 # a plan with no run changed nothing
    assert psg_bridge.changed_in_file(g, PLAN, "nowhere.py") == []


def test_plans_session_column_exists_and_session_plan_still_works(conn, tmp_path):
    assert "session_id" in [r[1] for r in conn.execute("PRAGMA table_info(Plans)")]
    g = _graph(tmp_path)
    conn.execute("INSERT INTO session_run (session_id, project, started_at, ended_at, refresh_state) VALUES ('sess-9', 'proj', '2026-09-16 09:00:00', '2026-09-16 10:30:00', 'done')")
    conn.commit()
    ctx = triggers._ctx(conn, "proj", "session:sess-9", g)
    assert ctx.plan["session_id"] == "sess-9"
