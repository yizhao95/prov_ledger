"""Smoke test for the installed `provledger` wheel — exercises the full
mini data-flow against a temp DB, with NO access to the repo source tree.

Run via scripts/test_packaging.sh (which builds the wheel, creates a fresh
venv, installs, and executes this file with that venv's python)."""
import sys
import tempfile
from pathlib import Path

# Hard requirement: this must be the installed wheel, not the repo source —
# the runner executes us from outside the repo with a clean sys.path.
import provledger                                        # noqa: E402
from provledger import api, db, state_machine            # noqa: E402, F401
from provledger.profiler import profile_records          # noqa: E402
from provledger.drift import detect_drift                # noqa: E402
from provledger.data_loop import run_data_decision_loop  # noqa: E402

print(f"import ok — provledger {provledger.__version__} "
      f"from {Path(provledger.__file__).parent}")
assert "site-packages" in provledger.__file__, "not running from the wheel!"

with tempfile.TemporaryDirectory() as td:
    conn = db.open_db(Path(td) / "orch.db")
    n = db.run_migrations(conn)
    print(f"migrations applied from the wheel: {n}")
    assert n >= 13

    # plan + typed steps
    r = api.initialize_plan(conn, "pkg smoke", [
        {"description": "ingest", "step_type": "COMMAND"},
        {"description": "verify", "step_type": "ANALYSIS"}])
    sid = r["step_ids"][0]
    api.start_step(conn, sid)
    api.append_log(conn, sid, "hello from the wheel")
    api.complete_step(conn, sid)

    # profile -> drift -> decision loop (Intent has `label`, Actual doesn't)
    actual = profile_records([{"a": 1, "b": 2.0}, {"a": 3, "b": None}],
                             dataset="d", plan_id=r["plan_id"])
    db.insert_data_profile(conn, actual)
    intent = [{"dataset": "d", "column_name": c, "dtype": t, "null_frac": 0.0}
              for c, t in {"a": "int", "b": "float", "label": "str"}.items()]
    drifts = detect_drift(intent, actual)
    assert any(d["kind"] == "column_dropped" and d["column"] == "label"
               for d in drifts), drifts

    results = run_data_decision_loop(
        conn, project="pkg-smoke", plan_id=r["plan_id"], step_id=r["step_ids"][1],
        prev_profile=intent, curr_profile=actual,
        downstream_consumers=["model.fit"], task_goal="smoke",
        decide=lambda ctx: {"action": "halt", "decision": "halt for smoke",
                            "rationale": "packaging test"},
        apply_action=lambda a, d: None, reprofile=lambda: actual)
    # two drifts fire: column_dropped(label) AND null_spike(b, 0.0 -> 0.5)
    assert results and all(x["outcome"] == "halted" for x in results), results

    n_dec = conn.execute("SELECT COUNT(*) FROM llm_decisions").fetchone()[0]
    n_led = conn.execute("SELECT COUNT(*) FROM LedgerEntries").fetchone()[0]
    assert n_dec == len(results) and n_led >= len(results), (n_dec, n_led)
    conn.close()

print("PACKAGE SMOKE TEST OK — plan/steps, migrations-from-wheel, "
      "profile→drift→decision→ledger all work from `pip install provledger`")
