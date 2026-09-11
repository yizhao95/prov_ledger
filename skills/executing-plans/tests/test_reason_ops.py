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


def test_reason_fill_unstated_string_becomes_null(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2,
                                      "reasons": [{"node_key": "nk_a", "text": "weekly grain"}, {"node_key": "nk_b", "text": "unstated"}]},
                      tmp_db, env_extra=psg_registry)
    assert r.returncode == 0, r.stderr
    assert _payload(r) == {"filled": 1, "unstated": 1, "unknown_keys": []}
    rows = sqlite3.connect(str(tmp_db)).execute(
        "SELECT node_key, text, source, tier FROM node_reason WHERE plan_id=? ORDER BY id", (pid,)).fetchall()
    assert rows == [("nk_a", "weekly grain", "agent", "stated"), ("nk_b", None, "agent", "stated")]
    # the filled slots leave the checklist
    r2 = run_script_fn("reason-slots", {"plan_id": pid, "project": "demo"}, tmp_db, env_extra=psg_registry)
    assert _payload(r2)["slots"] == []


def test_reason_fill_unknown_key_exits_6(seeded_plan, tmp_db, run_script_fn, psg_registry):
    pid = seeded_plan["plan_id"]
    r = run_script_fn("reason-fill", {"plan_id": pid, "project": "demo", "run_id": 2,
                                      "reasons": [{"node_key": "nk_zzz", "text": "x"}]}, tmp_db, env_extra=psg_registry)
    assert r.returncode == 6 and "nk_zzz" in (r.stdout + r.stderr)
    assert sqlite3.connect(str(tmp_db)).execute("SELECT COUNT(*) FROM node_reason").fetchone()[0] == 0


def test_reason_slots_without_graph_exits_6(seeded_plan, tmp_db, run_script_fn, tmp_path):
    reg = tmp_path / "empty.json"; reg.write_text('{"projects": []}')
    r = run_script_fn("reason-slots", {"plan_id": seeded_plan["plan_id"], "project": "demo"}, tmp_db,
                      env_extra={"PSG_REGISTRY_PATH": str(reg)})
    assert r.returncode == 6 and "no state graph" in (r.stdout + r.stderr)
