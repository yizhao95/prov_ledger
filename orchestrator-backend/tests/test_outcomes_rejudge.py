"""Survival is re-judged at every close (phase 5 Task 0-B): outcomes.backfill
recomputes survival.derive for every node/column expectation and appends an
outcome only when the signal differs from that expectation's latest survival
outcome. observed / none_available are still judged once."""
import json
import sys
from pathlib import Path

from orchestrator import db, outcomes

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

T = "2026-09-11T0{}:00:00+00:00"


def _expect(conn, plan_id, created_at="2026-09-11 01:00:00", **kw):
    base = dict(plan_id=plan_id, step_id=None, project="proj", target="pkg.m.clean", target_kind="node",
                claim="the filter settles here", channel="graph")
    base.update(kw)
    cur = conn.execute("INSERT INTO expectations (plan_id, step_id, project, target, target_kind, claim, channel, created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (base["plan_id"], base["step_id"], base["project"], base["target"], base["target_kind"],
                        base["claim"], base["channel"], created_at))
    conn.commit()
    return cur.lastrowid


def _graph(tmp_path):
    path = tmp_path / "g.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0"); ps.add_snapshot(c, 1, "nk_a", "pkg.m.clean", struct_sig="s1")
    ps.add_event(c, 1, 1, "node_added", "nk_a", created_at=T.format(0))
    c.commit()
    return path, c


def _change(c, run_id, plan_id, frm, to, hour):
    ps.add_run(c, run_id, plan_id=plan_id); ps.add_snapshot(c, run_id, "nk_a", "pkg.m.clean", struct_sig=to)
    ps.add_event(c, run_id, 1, "node_changed", "nk_a",
                 payload=json.dumps({"changed": ["struct_sig"], "struct_sig": {"from": frm, "to": to}}),
                 created_at=T.format(hour))
    c.commit()


def _signals(conn, eid):
    return [(json.loads(o["value_json"]).get("signal"), o["backfilled_by_plan"])
            for o in db.get_outcomes(conn, eid) if o["kind"] == "survival"]


def test_survival_is_rejudged_and_appended_only_when_the_signal_changes(conn, tmp_path):
    path, c = _graph(tmp_path)
    eid = _expect(conn, "P1")
    assert outcomes.backfill(conn, "proj", str(path), "P2")["survival"] == 1        # first judgment
    assert _signals(conn, eid) == [("untouched", "P2")]
    assert outcomes.backfill(conn, "proj", str(path), "P3")["survival"] == 0        # nothing changed: no second row
    _change(c, 2, "P3", "s1", "s2", 2)
    assert outcomes.backfill(conn, "proj", str(path), "P4")["survival"] == 1        # still 'untouched', but now changed_by P3: the evidence moved
    assert json.loads(db.get_outcomes(conn, eid)[-1]["value_json"])["changed_by"] == ["P3"]
    _change(c, 3, "P4", "s2", "s3", 3)
    assert outcomes.backfill(conn, "proj", str(path), "P5")["survival"] == 1        # two plans -> churned: appended
    assert _signals(conn, eid) == [("untouched", "P2"), ("untouched", "P4"), ("churned", "P5")]
    assert outcomes.backfill(conn, "proj", str(path), "P6")["survival"] == 0        # same verdict again: not appended
    _change(c, 4, "P6", "s3", "s1", 4)                                              # back to the value at the expectation
    assert outcomes.backfill(conn, "proj", str(path), "P7")["survival"] == 1
    assert _signals(conn, eid)[-1] == ("reverted", "P7")
    assert [o["kind"] for o in db.get_outcomes(conn, eid)] == ["survival"] * 4


def test_survival_pending_includes_expectations_that_already_have_an_outcome(conn, tmp_path):
    eid = _expect(conn, "P1")
    db.insert_outcome(conn, expectation_id=eid, kind="survival", value={"signal": "untouched"},
                      source="state_graph", tier="derived", backfilled_by_plan="P2")
    assert [e["id"] for e in db.get_pending_expectations(conn, "proj", exclude_plan_id="P3", kind="survival")] == [eid]
    # the closing plan's own expectations are never judged by its own close
    assert db.get_pending_expectations(conn, "proj", exclude_plan_id="P1", kind="survival") == []


def test_observed_and_none_available_are_still_judged_once(conn, tmp_path):
    path, _ = _graph(tmp_path)
    m = _expect(conn, "P1", target="latency", target_kind="metric", channel="metric:latency", claim="faster")
    assert outcomes.backfill(conn, "proj", str(path), "P2")["none_available"] == 1
    assert outcomes.backfill(conn, "proj", str(path), "P3")["none_available"] == 0
    assert len(db.get_outcomes(conn, m)) == 1
