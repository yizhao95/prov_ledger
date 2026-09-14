"""record-metric.sh / run-step.sh metrics_from_stdout — numeric observations reach the metrics table through the flows only."""
from __future__ import annotations

import json
import sqlite3


def _metrics(db_path):
    conn = sqlite3.connect(str(db_path)); conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM metrics ORDER BY id")]
    finally:
        conn.close()


def test_record_metric_op_records_a_number(seeded_plan, tmp_db, run_script_fn):
    pid = seeded_plan["plan_id"]; sid = seeded_plan["step_ids"][0]
    r = run_script_fn("record-metric", {"step_id": sid, "project": "demo", "name": "mean_net_revenue", "value": 43.6, "unit": "usd"}, tmp_db)
    assert r.returncode == 0, r.stderr
    marker = json.loads(r.stdout.splitlines()[-1])
    assert marker["ok"] is True and marker["op"] == "record-metric"
    rows = _metrics(tmp_db)
    assert len(rows) == 1 and rows[0]["name"] == "mean_net_revenue" and rows[0]["value"] == 43.6
    assert rows[0]["unit"] == "usd" and rows[0]["plan_id"] == pid and rows[0]["step_id"] == sid and rows[0]["source"] == "record-metric"


def test_record_metric_project_defaults_to_the_plans_project(seeded_plan, tmp_db, run_script_fn):
    pid = seeded_plan["plan_id"]
    conn = sqlite3.connect(str(tmp_db)); conn.execute("UPDATE Plans SET project='demo', project_source='declared' WHERE plan_id=?", (pid,)); conn.commit(); conn.close()
    r = run_script_fn("record-metric", {"plan_id": pid, "name": "ctr", "value": 1}, tmp_db)
    assert r.returncode == 0, r.stderr
    assert _metrics(tmp_db)[0]["project"] == "demo"
    # no project anywhere -> refused, nothing written
    r = run_script_fn("record-metric", {"name": "ctr", "value": 1}, tmp_db)
    assert r.returncode != 0 and "project" in r.stderr
    assert len(_metrics(tmp_db)) == 1


def test_record_metric_refuses_a_non_numeric_value(seeded_plan, tmp_db, run_script_fn):
    for bad in ("high", "", None, True, "nan"):
        r = run_script_fn("record-metric", {"plan_id": seeded_plan["plan_id"], "project": "demo", "name": "ctr", "value": bad}, tmp_db)
        assert r.returncode != 0 and "value" in r.stderr, bad
    assert _metrics(tmp_db) == []


def test_run_step_metrics_from_stdout(seeded_plan, tmp_db, run_script_fn):
    sid = seeded_plan["step_ids"][2]
    conn = sqlite3.connect(str(tmp_db)); conn.execute("UPDATE Plans SET project='demo', project_source='declared' WHERE plan_id=?", (seeded_plan["plan_id"],)); conn.commit(); conn.close()
    cmd = "echo start; echo 'metric name=mean_net_revenue value=43.60 unit=usd'; echo 'metric name=orders value=100000'; echo 'metric name=bad value=high'; echo done"
    r = run_script_fn("run-step", {"step_id": sid, "type": "COMMAND", "command": cmd, "metrics_from_stdout": True}, tmp_db)
    assert r.returncode == 0, r.stderr
    rows = _metrics(tmp_db)
    assert [(m["name"], m["value"], m["unit"], m["source"], m["step_id"]) for m in rows] == [
        ("mean_net_revenue", 43.6, "usd", "run-step", sid), ("orders", 100000.0, None, "run-step", sid)]
    assert rows[0]["project"] == "demo"
    # without the flag nothing is parsed
    r = run_script_fn("run-step", {"step_id": seeded_plan["step_ids"][1], "type": "COMMAND", "command": "echo 'metric name=x value=1'"}, tmp_db)
    assert r.returncode == 0 and len(_metrics(tmp_db)) == 2
