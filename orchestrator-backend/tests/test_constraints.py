"""orchestrator.constraints — anchored constraints and the close-time bypass record (E4-3)."""
import json
import sys
from pathlib import Path

import pytest

from orchestrator import api, constraints, db

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402
from test_reasons import _psg, _seed_registered_plan, _close  # noqa: E402


def _constraint(conn, subjects, **kw):
    cols = dict(project="proj", kind="constraint", subjects=json.dumps(subjects), keywords="[]",
                statement="exclude region X from the rollup", rationale="legal hold on region X",
                why_ref="https://wiki/decisions/42", why_visibility="shared")
    cols.update(kw)
    cur = conn.execute(f"INSERT INTO LedgerEntries ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       tuple(cols.values()))
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def psg_with_plan(tmp_path):
    return _psg(tmp_path)


@pytest.fixture
def registry(tmp_path, psg_with_plan):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": "proj", "repo": "/x", "db_path": psg_with_plan, "commit_sha": "c"}]}))
    return str(p)


def test_anchored_constraints_exact_match_and_restricted(conn):
    cid = _constraint(conn, ["nk_a", "orders.region"])
    _constraint(conn, ["nk_other"], statement="nk_a must keep the region filter", keywords='["nk_a"]')   # lexical only
    _constraint(conn, ["nk_a"], kind="decision")                                                        # not a constraint
    sup = _constraint(conn, ["nk_a"]); conn.execute("UPDATE LedgerEntries SET status='superseded' WHERE id=?", (sup,)); conn.commit()
    rid = _constraint(conn, ["nk_a"], why_visibility="restricted")
    got = constraints.anchored_constraints(conn, "proj", ["nk_a"])
    assert [c["id"] for c in got] == [cid, rid]
    assert got[0]["rationale"].startswith("legal hold") and got[0]["subjects"] == ["nk_a", "orders.region"]
    assert got[1]["rationale"] is None and got[1]["why_ref"] == "https://wiki/decisions/42"
    assert constraints.anchored_constraints(conn, "proj", ["nk_zzz"]) == []
    assert constraints.anchored_constraints(conn, "proj", []) == []


def test_e4_3_bypass_recorded_visibly_and_close_still_completes(conn, psg_with_plan, registry):
    cid = _constraint(conn, ["nk_a"])
    plan_id, review = _seed_registered_plan(conn, "P1", registry=registry)        # impact_context: none -> unread
    out = _close(conn, plan_id, registry)
    assert out["plan_status"] == "COMPLETED" and out["constraints_bypassed"] == 1
    rows = [dict(r) for r in conn.execute("SELECT * FROM node_reason_v WHERE plan_id=? AND kind='constraint_ref'", (plan_id,))]
    assert len(rows) == 1 and rows[0]["node_key"] == "nk_a"
    assert rows[0]["text"] == f"constraint_bypassed:{cid}: exclude region X from the rollup"
    assert rows[0]["source"] == "system" and rows[0]["tier"] == "derived"
    assert "[CONSTRAINT BYPASSED] 1" in (db.get_step(conn, review)["log_context"] or "")
    assert db.get_plan(conn, plan_id)["status"] == "COMPLETED"


def test_no_bypass_when_surfaced(conn, psg_with_plan, registry):
    cid = _constraint(conn, ["nk_a"])
    plan_id, review = _seed_registered_plan(conn, "P1", registry=registry)
    db.set_plan_impact_context(conn, plan_id, json.dumps({"constraint_ids": [cid], "symbols": []}))
    out = _close(conn, plan_id, registry)
    assert out["constraints_bypassed"] == 0
    assert [dict(r) for r in conn.execute("SELECT * FROM node_reason_v WHERE plan_id=? AND kind='constraint_ref'", (plan_id,))] == []
    assert "[CONSTRAINT BYPASSED]" not in (db.get_step(conn, review)["log_context"] or "")


def test_bypass_only_for_nodes_the_plan_touched(conn, psg_with_plan, registry):
    _constraint(conn, ["nk_untouched"])                # anchored elsewhere: not this plan's business
    plan_id, _ = _seed_registered_plan(conn, "P1", registry=registry)
    assert _close(conn, plan_id, registry)["constraints_bypassed"] == 0


def test_bypass_idempotent(conn, psg_with_plan, registry):
    _constraint(conn, ["nk_a"])
    plan_id, review = _seed_registered_plan(conn, "P1", registry=registry)
    _close(conn, plan_id, registry)
    n = constraints.bypassed_at_close(conn, project="proj", plan_id=plan_id, psg_db_path=psg_with_plan,
                                      review_step_id=review, commit=True)
    assert n == 0 and len([dict(r) for r in conn.execute("SELECT * FROM node_reason_v WHERE plan_id=? AND kind='constraint_ref'", (plan_id,))]) == 1


def test_anchored_constraints_match_qualified_names_too(conn):
    cid = _constraint(conn, ["orders.region"])
    assert [c["id"] for c in constraints.anchored_constraints(conn, "proj", [], qualified_names=["orders.region"])] == [cid]
    assert [c["id"] for c in constraints.anchored_constraints(conn, "proj", ["nk_zzz"], qualified_names=["orders.region"])] == [cid]
    assert constraints.anchored_constraints(conn, "proj", [], qualified_names=["nope"]) == []
