"""reason-slots.sh / reason-fill.sh — the review agent's closed-form checklist ops."""
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


def test_reason_slots_prints_closed_checklist(seeded_plan, tmp_db, run_script_fn, psg_registry):
    r = run_script_fn("reason-slots", {"plan_id": seeded_plan["plan_id"], "project": "demo"}, tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    p = _payload(r)
    assert [s["node_key"] for s in p["slots"]] == ["nk_a", "nk_b"]
    assert "1. pkg.m.load" in p["checklist"] and "2. pkg.m.load:df.amount" in p["checklist"] and "unstated" in p["checklist"]
    assert json.loads(r.stdout.splitlines()[-1])["ok"] is True


def test_reason_fill_three_shapes_and_the_old_text_shape_exits_2(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    c = sqlite3.connect(str(tmp_db))
    c.execute("INSERT INTO utterance (session_id, project, plan_id, text, occurred_at, hash) VALUES ('s', 'demo', ?, 'keep the weekly grain for load', '2026-09-15 10:00:00', 'h')", (pid,))
    c.commit(); uid = c.execute("SELECT id FROM utterance").fetchone()[0]; c.close()
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2,
                                      "reasons": [{"node_key": "nk_a", "text": "weekly grain"}]}, tmp_db, env_extra=psg_registry)
    assert r.returncode == 2 and '"text" is not accepted' in (r.stdout + r.stderr)
    assert sqlite3.connect(str(tmp_db)).execute("SELECT COUNT(*) FROM change_reason").fetchone()[0] == 0
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2,
                                      "reasons": [{"node_key": "nk_a", "utterance_id": uid, "span": [0, 21]},
                                                  {"node_key": "nk_b", "unstated": True}]},
                      tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    assert _payload(r) == {"filled": 1, "stated": 1, "asserted": 0, "unstated": 1, "adopted": 0, "unknown_keys": []}
    rows = sqlite3.connect(str(tmp_db)).execute(
        "SELECT node_key, tier, recorded_by, verbatim_utterance_id, verbatim_end FROM change_reason WHERE plan_id=? ORDER BY id", (pid,)).fetchall()
    assert rows == [("nk_a", "stated", "agent", uid, 21), ("nk_b", "unstated", "agent", None, None)]
    r2 = run_script_fn("reason-slots", {"plan_id": pid, "project": "demo"}, tmp_db, env_extra=psg_registry)
    assert _payload(r2)["slots"] == []


def test_reason_fill_interpretation_is_asserted_even_from_a_human(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2, "source": "human",
                                      "reasons": [{"node_key": "nk_a", "interpretation": "finance reconciles weekly"},
                                                  {"node_key": "nk_b", "interpretation": "new column for the rollup"}]},
                      tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    rows = sqlite3.connect(str(tmp_db)).execute("SELECT node_key, tier, recorded_by, interpretation FROM change_reason ORDER BY id").fetchall()
    assert rows == [("nk_a", "asserted", "human", "finance reconciles weekly"), ("nk_b", "asserted", "human", "new column for the rollup")]


def test_reason_slots_draft_proposes_spans(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    c = sqlite3.connect(str(tmp_db))
    c.execute("INSERT INTO utterance (session_id, project, plan_id, text, occurred_at, hash) VALUES ('s', 'demo', ?, 'load must keep paid orders only', '2026-09-15 10:00:00', 'h')", (pid,))
    c.commit(); c.close()
    r = run_script_fn("reason-slots", {"plan_id": pid, "project": "demo", "draft": True}, tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    p = _payload(r)
    by_key = {d["node_key"]: d["candidates"] for d in p["draft"]}
    assert by_key["nk_a"] and by_key["nk_a"][0]["span"] == [0, 31] and by_key["nk_a"][0]["preview"].startswith("load must")
    assert p["auto_filled"] == []


def test_reason_fill_unknown_key_exits_6(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2,
                                      "reasons": [{"node_key": "nk_zzz", "interpretation": "x"}]}, tmp_db, env_extra=psg_registry)
    assert r.returncode == 6 and "nk_zzz" in (r.stdout + r.stderr)
    assert sqlite3.connect(str(tmp_db)).execute("SELECT COUNT(*) FROM change_reason").fetchone()[0] == 0


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
                                      "reasons": [{"node_key": "nk_a", "interpretation": "kept weekly grain as before", "because": [old]},
                                                  {"node_key": "nk_b", "unstated": True}]},
                      tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    assert _payload(r)["adopted"] == 1
    rows = sqlite3.connect(str(tmp_db)).execute("SELECT reason_id, plan_id, node_key, via, by FROM influence").fetchall()
    assert rows == [(old, pid, "nk_a", "reason_because", "agent")]
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2,
                                      "reasons": [{"node_key": "nk_a", "interpretation": "x", "because": [9999]}]}, tmp_db, env_extra=psg_registry)
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
