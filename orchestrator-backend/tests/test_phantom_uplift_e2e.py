"""Spec §3.6 criterion on examples/phantom-uplift: a profile_drift expectation
on the checkout-orders dataset gets an observed outcome containing
column_dropped when the next plan closes (promo_discount vanished upstream)."""
import json
import random
import sys
from pathlib import Path

from orchestrator import api, db, outcomes
from orchestrator.profiler import profile_records

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "phantom-uplift"
DATASET = "checkout_orders"

# The feeds are gitignored build products of gen_upstream.py; a fresh clone has
# none. Generate them in-memory with the demo's own deterministic generator
# (seed 42) so this test never depends on `make demo` having run (cf. FL-016).
sys.path.insert(0, str(EXAMPLE))
import gen_upstream  # noqa: E402


def _feeds() -> tuple[list[dict], list[dict]]:
    fixed = gen_upstream.generate(random.Random(gen_upstream.SEED_THIS_WEEK), gen_upstream.THIS_WEEK_DAYS, 100_000)
    drifted = [{k: v for k, v in o.items() if k != "promo_discount"} for o in fixed]
    return fixed, drifted


def _plan(conn, plan_id, goal):
    db.insert_plan(conn, plan_id, goal)
    db.insert_step(conn, f"{plan_id}-A", plan_id, "COMMAND: ingest + profile", 0, status="COMPLETED")
    return db.insert_review_step(conn, plan_id)


def test_phantom_uplift_expectation_gets_observed_column_dropped(conn, tmp_path):
    fixed, drifted = _feeds()
    assert "promo_discount" in fixed[0] and "promo_discount" not in drifted[0]
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "phantom-uplift", "repo": str(EXAMPLE), "db_path": str(tmp_path / "absent.db"), "commit_sha": "c"}]}))

    # plan 1: ingest the healthy feed, claim the column stays
    _plan(conn, "PU1", "phantom-uplift weekly rollup")
    rows = [{**r, "project": "phantom-uplift", "plan_id": "PU1", "step_id": "PU1-A"} for r in profile_records(fixed, dataset=DATASET)]
    db.insert_data_profile(conn, rows)
    conn.execute("UPDATE data_profile SET observed_at='2026-09-11 00:30:00' WHERE plan_id='PU1'")
    eid = conn.execute("INSERT INTO expectations (plan_id, step_id, project, target, target_kind, claim, channel, created_at) "
                       "VALUES ('PU1','PU1-A','phantom-uplift',?,'dataset','promo_discount stays present in checkout orders','profile_drift','2026-09-11 01:00:00')",
                       (DATASET,)).lastrowid
    conn.commit()

    # plan 2: next week's feed silently lost the column; its reviewed close backfills plan 1's expectation
    _plan(conn, "PU2", "phantom-uplift weekly rollup, week 2")
    rows = [{**r, "project": "phantom-uplift", "plan_id": "PU2", "step_id": "PU2-A"} for r in profile_records(drifted, dataset=DATASET)]
    db.insert_data_profile(conn, rows)
    conn.execute("UPDATE data_profile SET observed_at='2026-09-11 02:00:00' WHERE plan_id='PU2'"); conn.commit()
    out = api.review_and_complete(conn, "PU2", registry_path=str(reg))
    db.update_step_status(conn, out["review_child_step_id"], "COMPLETED", set_completed=True)
    out = api.review_and_complete(conn, "PU2", registry_path=str(reg))
    assert out["plan_status"] == "COMPLETED"
    o = db.get_outcomes(conn, eid)
    assert len(o) == 1 and o[0]["kind"] == "observed" and o[0]["tier"] == "observed" and o[0]["backfilled_by_plan"] == "PU2"
    v = json.loads(o[0]["value_json"])
    assert any(d["kind"] == "column_dropped" and d["column"] == "promo_discount" for d in v["drifts"])
    # the SQL a reviewer would paste into the PR
    row = conn.execute("SELECT e.target, e.claim, o.kind, o.value_json FROM outcomes o JOIN expectations e ON e.id=o.expectation_id").fetchone()
    assert row["target"] == DATASET and "column_dropped" in row["value_json"]
