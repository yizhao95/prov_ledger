"""reason-slots.sh / reason-fill.sh — the review agent's close-time reason ops, one reason per task."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from conftest import ORCH_ROOT  # noqa: E402

sys.path.insert(0, str(ORCH_ROOT / "tests"))
import _psg_schema as ps  # noqa: E402


@pytest.fixture
def psg_registry(tmp_path, seeded_plan):
    """A PSG graph whose run 2 is attributed to the seeded plan: nk_a changed, nk_b added."""
    plan_id = seeded_plan["plan_id"]
    path = tmp_path / "demo-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_snapshot(c, 1, "nk_a", "pkg.m.load"); ps.add_event(c, 1, 1, "node_added", "nk_a")
    ps.add_run(c, 2, plan_id=plan_id, step_id=f"{plan_id}-REVIEW.1")
    ps.add_snapshot(c, 2, "nk_a", "pkg.m.load", struct_sig="s2")
    ps.add_snapshot(c, 2, "nk_b", "pkg.m.load:df.amount", ntype="column")
    ps.add_event(c, 2, 1, "node_matched", "nk_a", '{"via": "qualname"}')
    ps.add_event(c, 2, 2, "node_changed", "nk_a", '{"changed": ["struct_sig"]}')
    ps.add_event(c, 2, 3, "node_added", "nk_b")
    c.commit(); c.close()
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "demo", "repo": "/x", "db_path": str(path), "commit_sha": "c"}]}))
    return {"PSG_REGISTRY_PATH": str(reg)}


def _payload(r):
    return json.loads("\n".join(r.stdout.splitlines()[:-1]))


def _utt(tmp_db, pid, text):
    c = sqlite3.connect(str(tmp_db))
    c.execute("INSERT INTO utterance (session_id, project, plan_id, text, occurred_at, hash) VALUES ('s', 'demo', ?, ?, '2026-09-15 10:00:00', 'h')", (pid, text))
    c.commit(); uid = c.execute("SELECT MAX(id) FROM utterance").fetchone()[0]; c.close()
    return uid


def _rows(tmp_db, sql, *args):
    return sqlite3.connect(str(tmp_db)).execute(sql, args).fetchall()


def test_reason_slots_prints_the_changed_nodes_the_root_and_the_task_reasons(seeded_plan, tmp_db, run_script_fn, psg_registry):
    """Close-time reasons are per task (task-level redesign, step 3a): the
    agent sees what the plan changed (to group, when it did several things),
    the plan's root, and what is already written — not a slot per node."""
    r = run_script_fn("reason-slots", {"plan_id": seeded_plan["plan_id"], "project": "demo"}, tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    p = _payload(r)
    assert [n["node_key"] for n in p["nodes"]] == ["nk_a", "nk_b"]
    assert p["root"] is None and p["tasks"] == []
    assert "2 changed node(s)" in p["checklist"] and "0 task reason(s)" in p["checklist"]
    assert "slots" not in p and "draft" not in p
    assert json.loads(r.stdout.splitlines()[-1])["ok"] is True


def test_reason_fill_one_task_three_shapes_and_the_old_list_exits_2(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    uid = _utt(tmp_db, pid, "keep the weekly grain for load")
    for old in ({"reasons": [{"node_key": "nk_a", "unstated": True}]}, {"tasks": [{"text": "weekly grain"}]}):
        r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2, **old}, tmp_db, env_extra=psg_registry)
        assert r.returncode == 2, r.stdout + r.stderr
    assert "per task" in run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2,
                                                       "reasons": [{"node_key": "nk_a", "unstated": True}]},
                                       tmp_db, env_extra=psg_registry).stderr
    assert _rows(tmp_db, "SELECT COUNT(*) FROM change_reason") == [(0,)]
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2,
                                      "tasks": [{"utterance_id": uid, "span": [0, 21]}]}, tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    got = _payload(r)
    assert (got["tasks"], got["stated"], got["unknown_nodes"]) == (1, 1, [])
    assert _rows(tmp_db, "SELECT node_key, tier, recorded_by, verbatim_utterance_id, verbatim_end FROM change_reason WHERE plan_id=?",
                 pid) == [(None, "stated", "agent", uid, 21)]
    r2 = run_script_fn("reason-slots", {"plan_id": pid, "project": "demo"}, tmp_db, env_extra=psg_registry)
    assert [t["tier"] for t in _payload(r2)["tasks"]] == ["stated"]


def test_reason_fill_interpretation_is_asserted_even_from_a_human(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2, "source": "human",
                                      "tasks": [{"interpretation": "finance reconciles weekly"}]},
                      tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    assert _rows(tmp_db, "SELECT node_key, tier, recorded_by, interpretation FROM change_reason") == \
        [(None, "asserted", "human", "finance reconciles weekly")]


def test_reason_fill_several_tasks_assign_nodes_and_need_a_bundle(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    two = [{"interpretation": "the weekly grain was asked for"},
           {"interpretation": "the rollup needed the amount", "nodes": ["pkg.m.load:df.amount"]}]
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2, "tasks": two}, tmp_db, env_extra=psg_registry)
    assert r.returncode == 2 and "bundle" in r.stderr
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2, "tasks": two,
                                      "bundle": "both came out of the same review"}, tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    assert _payload(r)["tasks"] == 2
    assert _rows(tmp_db, "SELECT node_key FROM reason_node") == [("nk_b",)]
    assert _rows(tmp_db, "SELECT plan_id, basis FROM plan_bundle") == [(pid, "both came out of the same review")]


def test_reason_slots_draft_proposes_sentences_for_the_task(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    _utt(tmp_db, pid, "load must keep paid orders only")
    r = run_script_fn("reason-slots", {"plan_id": pid, "project": "demo", "draft": True}, tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    d = _payload(r)["draft"]
    assert d and d[0]["span"] == [0, 31] and d[0]["preview"].startswith("load must")


def test_reason_fill_unknown_node_exits_6(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2, "bundle": "x",
                                      "tasks": [{"interpretation": "a"}, {"interpretation": "b", "nodes": ["nk_zzz"]}]},
                      tmp_db, env_extra=psg_registry)
    assert r.returncode == 6 and "nk_zzz" in (r.stdout + r.stderr)
    assert _rows(tmp_db, "SELECT COUNT(*) FROM change_reason") == [(0,)]


def test_reason_slots_without_graph_exits_6(seeded_plan, tmp_db, run_script_fn, tmp_path):
    reg = tmp_path / "empty.json"; reg.write_text('{"projects": []}')
    r = run_script_fn("reason-slots", {"plan_id": seeded_plan["plan_id"], "project": "demo"}, tmp_db,
                      env_extra={"PSG_REGISTRY_PATH": str(reg)})
    assert r.returncode == 6 and "no state graph" in (r.stdout + r.stderr)


# ── DP phase 2 Task 3: because → adopted; headline-respond op ────────────────
def test_reason_fill_because_writes_influence(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    c = sqlite3.connect(str(tmp_db))
    c.execute("INSERT INTO change_reason (project, plan_id, node_key, kind, role, interpretation, occurred_at, recorded_by, tier, hash) "
              "VALUES ('demo', 'P0', 'nk_a', 'technical', 'reason', 'earlier: keep weekly grain', '2026-09-15 09:00:00', 'agent', 'asserted', 'h')")
    old = c.execute("SELECT id FROM change_reason").fetchone()[0]; c.commit(); c.close()
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2,
                                      "tasks": [{"interpretation": "kept weekly grain as before", "because": [old]}]},
                      tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    assert _payload(r)["adopted"] == 1
    assert _rows(tmp_db, "SELECT reason_id, plan_id, node_key, via, by FROM influence") == [(old, pid, None, "reason_because", "agent")]
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2,
                                      "tasks": [{"interpretation": "x", "because": [9999]}]}, tmp_db, env_extra=psg_registry)
    assert r.returncode == 2 and "9999" in (r.stdout + r.stderr)


def test_headline_respond_op_answers_once_and_adopts_cites(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    c = sqlite3.connect(str(tmp_db))
    c.execute("INSERT INTO change_reason (project, plan_id, node_key, kind, role, statement, occurred_at, recorded_by, tier, hash) "
              "VALUES ('demo', 'ledger', 'nk_a', 'organizational', 'constraint', 'keep paid only', '2026-09-15 09:00:00', 'human', 'asserted', 'h')")
    cid = c.execute("SELECT id FROM change_reason").fetchone()[0]
    c.execute("UPDATE Plans SET project='demo' WHERE plan_id=?", (pid,))
    c.execute("INSERT INTO headline (project, plan_id, findings_json) VALUES ('demo', ?, ?)",
              (pid, json.dumps({"findings": [{"id": "active_constraint:nk_a:1", "layer": "self", "kind": "active_constraint", "tier": "asserted",
                                              "severity": "blocking", "text": "keep paid only", "anchor": "pkg.m.load", "evidence": {"reason_id": cid}, "hard": False}],
                                "summary": {"targets": 1, "layers": 2, "findings": 1, "blocking": 1, "warning": 0, "info": 0, "unanswered": 1, "shown": 1}})))
    c.commit(); c.close()
    r = run_script_fn("headline-respond", {"plan_id": pid, "finding_id": "active_constraint:nk_a:1", "action": "proceed",
                                           "rationale": "the filter moves downstream", "by": "agent"}, tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    p = _payload(r)
    assert p["summary"]["unanswered"] == 0 and p["summary"]["adopted"] == 1
    rows = sqlite3.connect(str(tmp_db)).execute("SELECT reason_id, via, by FROM influence").fetchall()
    assert rows == [(cid, "headline_response", "agent")]
    r2 = run_script_fn("headline-respond", {"plan_id": pid, "finding_id": "active_constraint:nk_a:1", "action": "revise", "by": "human"}, tmp_db, env_extra=psg_registry)
    assert r2.returncode == 2 and "already answered" in (r2.stdout + r2.stderr)
