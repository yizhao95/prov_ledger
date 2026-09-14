"""orchestrator.outcomes — close-time backfill of expectations (spec §3.4/§3.5)."""
import json
import sys
from pathlib import Path

import pytest

from orchestrator import api, db, outcomes

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402


def _profile(conn, plan_id, cols, when):
    db.insert_data_profile(conn, [{"project": "proj", "plan_id": plan_id, "step_id": f"{plan_id}-A", "dataset": "orders",
                                   "column_name": c, "dtype": t, "null_frac": 0.0, "row_count": 10, "distinct_count": 5}
                                  for c, t in cols])
    conn.execute("UPDATE data_profile SET observed_at=? WHERE plan_id=?", (when, plan_id)); conn.commit()


def _expect(conn, plan_id, **kw):
    base = dict(plan_id=plan_id, step_id=None, project="proj", target="orders", target_kind="dataset",
                claim="promo_discount stays present", channel="profile_drift")
    base.update(kw)
    cur = conn.execute("INSERT INTO expectations (plan_id, step_id, project, target, target_kind, claim, channel, created_at) "
                       "VALUES (?,?,?,?,?,?,?,'2026-09-11 01:00:00')",
                       (base["plan_id"], base["step_id"], base["project"], base["target"], base["target_kind"], base["claim"], base["channel"]))
    conn.commit()
    return cur.lastrowid


def test_profile_drift_observed_with_column_dropped(conn):
    _profile(conn, "P1", [("amount", "float"), ("promo_discount", "float")], "2026-09-11 00:30:00")
    eid = _expect(conn, "P1")
    _profile(conn, "P2", [("amount", "float")], "2026-09-11 02:00:00")
    counts = outcomes.backfill(conn, "proj", None, "P2")
    assert counts["observed"] == 1
    o = db.get_outcomes(conn, eid)
    assert [x["kind"] for x in o] == ["observed"] and o[0]["tier"] == "observed" and o[0]["backfilled_by_plan"] == "P2"
    v = json.loads(o[0]["value_json"])
    assert "column_dropped" in v["kinds"] and any(d["column"] == "promo_discount" for d in v["drifts"])
    assert outcomes.backfill(conn, "proj", None, "P3") == {"observed": 0, "survival": 0, "none_available": 0, "skipped_graph_channel": 0, "errors": 0}   # idempotent


def test_profile_drift_without_after_snapshot_is_none_available(conn):
    _profile(conn, "P1", [("amount", "float")], "2026-09-11 00:30:00")
    eid = _expect(conn, "P1")
    outcomes.backfill(conn, "proj", None, "P2")
    o = db.get_outcomes(conn, eid)[0]
    assert o["kind"] == "none_available" and "before and after" in o["reason"]


def test_metric_and_none_channels(conn):
    m = _expect(conn, "P1", target="ctr", target_kind="metric", channel="metric:ctr", claim="ctr up 2%")
    n = _expect(conn, "P1", target="orders", channel="none", claim="no way to observe this yet")
    counts = outcomes.backfill(conn, "proj", None, "P2")
    assert counts["none_available"] == 2
    assert "no metric 'ctr' observed before" in db.get_outcomes(conn, m)[0]["reason"]   # phase 7: MetricChannel answers
    assert db.get_outcomes(conn, n)[0]["reason"] == "no way to observe this yet"


def test_graph_channel_is_survival_only(conn, tmp_path):
    path = tmp_path / "g.db"; c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0"); ps.add_snapshot(c, 1, "nk_a", "pkg.m.load")
    ps.add_event(c, 1, 1, "node_added", "nk_a", created_at="2026-09-11T00:00:01+00:00"); c.commit(); c.close()
    eid = _expect(conn, "P1", target="pkg.m.load", target_kind="node", channel="graph", claim="load keeps its shape")
    counts = outcomes.backfill(conn, "proj", str(path), "P2")
    assert counts["survival"] == 1 and counts["skipped_graph_channel"] == 1
    o = db.get_outcomes(conn, eid)
    assert [x["kind"] for x in o] == ["survival"] and o[0]["tier"] == "derived"
    assert json.loads(o[0]["value_json"])["signal"] == "untouched" and "weak" in o[0]["reason"]


def test_survival_degrades_when_graph_missing(conn, tmp_path):
    eid = _expect(conn, "P1", target="pkg.m.load", target_kind="node", channel="graph", claim="c")
    outcomes.backfill(conn, "proj", str(tmp_path / "absent.db"), "P2")
    o = db.get_outcomes(conn, eid)[0]
    assert o["kind"] == "none_available" and "state graph unavailable" in o["reason"]


def test_reviewed_close_triggers_backfill(conn, tmp_path):
    """The registered-project close (Task 5's path) backfills other plans' expectations."""
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "proj", "repo": "/x", "db_path": str(tmp_path / "absent.db"), "commit_sha": "c"}]}))
    _profile(conn, "P1", [("amount", "float"), ("promo_discount", "float")], "2026-09-11 00:30:00")
    eid = _expect(conn, "P1")
    db.insert_plan(conn, "P2", "improve proj rollup")
    db.insert_step(conn, "P2-A", "P2", "CODE: work", 0, status="COMPLETED")
    db.insert_review_step(conn, "P2")
    out = api.review_and_complete(conn, "P2", registry_path=str(reg))
    _profile(conn, "P2", [("amount", "float")], "2026-09-11 02:00:00")
    db.update_step_status(conn, out["review_child_step_id"], "COMPLETED", set_completed=True)
    out = api.review_and_complete(conn, "P2", registry_path=str(reg))
    assert out["plan_status"] == "COMPLETED" and out["outcomes_backfilled"]["observed"] == 1
    assert "column_dropped" in json.loads(db.get_outcomes(conn, eid)[0]["value_json"])["kinds"]
