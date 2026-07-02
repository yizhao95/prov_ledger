#!/usr/bin/env python3
"""One-command demo: a silent upstream class-drop, caught by the data contract.

The arc (all against the REAL orchestrator backbone, in a throwaway DB):

  v1      ingest the drifted feed -> cluster: "6 clusters formed, exit_code=0",
          every step COMPLETED (green) — but segment purity silently collapsed.
  CATCH   the verify step compares the declared data Intent against the runtime
          Actual profile -> MISMATCH: upstream silently dropped `class`.
  REVISE  the decision loop records the LLM decision (and the anti-pattern in
          the ledger, so it is never repeated), then revises the plan through
          the backbone (deviation + sub-steps) — never by editing state.
  v2      re-ingest the fixed feed -> contract VERIFIED, purity recovers.

Everything lands in ./demo-orchestrator.db (NEVER your real orchestrator DB),
so the read-only dashboard can replay the whole story afterwards.

Usage:  python run_demo.py           (or `make demo` from the repo root)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "orchestrator-backend"))

from orchestrator import api, db as odb                    # noqa: E402
from orchestrator.data_loop import run_data_decision_loop  # noqa: E402
from orchestrator.drift import detect_drift                # noqa: E402
from orchestrator.profiler import profile_records          # noqa: E402

PROJECT = "silent-class-drop"
DATASET = "upstream_events"
DB_PATH = HERE / "demo-orchestrator.db"
TRUTH = HERE / "ground_truth.csv"

# The cluster step's data Intent: what the downstream job REQUIRES of the feed.
DECLARED_SCHEMA = {
    "box_id": "int", "xmin": "float", "xmax": "float",
    "ymin": "float", "ymax": "float", "class": "str",
}

# ── tiny ANSI layer (NO_COLOR-aware) ──────────────────────────────────────────
_TTY = os.environ.get("NO_COLOR") is None
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s
def red(s): return _c("1;31", s)
def green(s): return _c("1;32", s)
def yellow(s): return _c("1;33", s)
def bold(s): return _c("1", s)
def dim(s): return _c("2", s)


def intent_rows() -> list[dict]:
    """The declared Intent, shaped like profile rows so drift-detection can
    compare it directly against the runtime Actual."""
    return [{"dataset": DATASET, "column_name": col, "dtype": dtype,
             "null_frac": 0.0} for col, dtype in DECLARED_SCHEMA.items()]


def run_cluster_job(source: Path) -> tuple[str, float]:
    """Run the (provLedger-unaware) business job; returns (stdout, purity)."""
    proc = subprocess.run(
        [sys.executable, str(HERE / "cluster_and_eval.py"), str(source), str(TRUTH)],
        capture_output=True, text=True, check=True,
    )
    purity = next(float(line.split("purity=")[1].split()[0])
                  for line in proc.stdout.splitlines() if line.startswith("RESULT"))
    return proc.stdout, purity


def contract_panel(actual: list[dict]) -> list[dict]:
    """Print the Intent | Actual panel; returns the drifts (empty = verified)."""
    drifts = detect_drift(intent_rows(), actual)
    bad_cols = {d["column"] for d in drifts}
    actual_by_col = {r["column_name"]: r for r in actual}

    verdict = (red("🔴 MISMATCH") if drifts else green("✅ VERIFIED"))
    print(f"\n  ┌─ data contract · {DATASET} " + "─" * 30)
    print(f"  │ {'column':<10} {'Intent':<10} {'Actual':<14} ")
    print(f"  │ {'-'*10} {'-'*10} {'-'*14}")
    for col, want in DECLARED_SCHEMA.items():
        a = actual_by_col.get(col)
        got = a["dtype"] if a else "— missing —"
        line = f"  │ {col:<10} {want:<10} {got:<14}"
        print(red(line + "  ← required, absent from feed") if col in bad_cols
              else line + dim("  ok"))
    print(f"  │")
    print(f"  │ verdict: {verdict}"
          + (f"  ({', '.join(d['kind'] + ':' + d['column'] for d in drifts)})"
             if drifts else ""))
    print(f"  └" + "─" * 52)
    return drifts


def main() -> int:
    print(bold("\n═══ provLedger demo — the silent class-drop ═══\n"))

    # Fresh, throwaway DB + freshly generated deterministic data (seed 42).
    DB_PATH.unlink(missing_ok=True)
    subprocess.run([sys.executable, str(HERE / "gen_upstream.py")],
                   check=True, capture_output=True)
    conn = odb.open_db(DB_PATH)
    odb.run_migrations(conn)

    plan = api.initialize_plan(
        conn,
        "Segment shelf detection events into 6 product-category groups",
        ["Ingest upstream detection events",
         "Cluster events into 6 category segments and evaluate purity",
         "Verify the data contract (Intent vs Actual) and sign off"],
        user_query="Segment the shelf-monitor detection feed into 6 "
                   "product-category groups and report segment purity.",
    )
    plan_id = plan["plan_id"]
    s_ingest, s_cluster, s_verify = plan["step_ids"]

    # ── v1: the false success ────────────────────────────────────────────────
    print(bold("── v1 · run against the current upstream feed ──"))
    api.start_step(conn, s_ingest)
    events_v1 = json.loads((HERE / "upstream_drifted.json").read_text())
    actual_v1 = profile_records(events_v1, dataset=DATASET, project=PROJECT,
                                plan_id=plan_id, step_id=s_ingest)
    odb.insert_data_profile(conn, actual_v1)
    api.append_log(conn, s_ingest,
                   f"ingested {len(events_v1)} events from upstream_drifted.json; "
                   f"runtime profile captured ({len(actual_v1)} columns)")
    api.complete_step(conn, s_ingest)
    print(f"  step 1/3 ingest   {green('COMPLETED')}  "
          f"({len(events_v1)} events)")

    api.start_step(conn, s_cluster)
    out_v1, purity_v1 = run_cluster_job(HERE / "upstream_drifted.json")
    api.append_log(conn, s_cluster, out_v1)
    api.complete_step(conn, s_cluster)
    print(f"  step 2/3 cluster  {green('COMPLETED')}  "
          f"6 clusters formed. exit_code=0")
    print(dim(f"           (eval harness: segment purity {purity_v1:.2f} "
              f"— nobody is looking at this yet)"))

    # ── the catch: Intent vs Actual ──────────────────────────────────────────
    print(bold("\n── verify · declared Intent vs runtime Actual ──"))
    api.start_step(conn, s_verify)
    drifts_v1 = contract_panel(actual_v1)
    assert drifts_v1, "demo invariant: the drifted feed must MISMATCH"

    # ── the decision loop: record -> revise through the backbone -> re-verify ─
    print(bold("\n── revise · LLM decision, recorded + applied through the backbone ──"))
    holder: dict = {}

    def decide(ctx: dict) -> dict:
        d = ctx["drift"]
        return {
            "action": "coerce_upstream",
            "decision": f"Upstream silently dropped `{d['column']}` — restore the "
                        "catalog-join enrichment upstream, then re-ingest and re-cluster.",
            "rationale": "cluster_and_eval.build_features degrades to geometry-only "
                         "features without `class`; the job still exits 0 but segment "
                         "purity collapses. Fix the source, don't patch downstream.",
        }

    def apply_action(action: str, d: dict) -> None:
        # ALL state changes go through the backbone: a recorded deviation that
        # inserts sub-steps, then normal step execution. Nothing edits state.
        dev = api.evaluate_and_update_plan(
            conn, deviation_detected=True, target_step_id=s_verify,
            justification=f"data contract MISMATCH ({d['kind']}: `{d['column']}`): "
                          "upstream dropped the class enrichment; restoring it and re-running.",
            new_sub_steps=["Re-ingest events from the fixed upstream feed",
                           "Re-run clustering on the restored feed"])
        assert dev["accepted"], dev
        sub_ingest, sub_cluster = dev["new_step_ids"]
        print(f"  deviation recorded (rev {dev['revision_count']}) "
              f"→ sub-steps {sub_ingest.split('-')[-1]}, {sub_cluster.split('-')[-1]}")

        api.start_step(conn, sub_ingest)
        events_v2 = json.loads((HERE / "upstream_fixed.json").read_text())
        holder["actual_v2"] = profile_records(events_v2, dataset=DATASET,
                                              project=PROJECT, plan_id=plan_id,
                                              step_id=sub_ingest)
        odb.insert_data_profile(conn, holder["actual_v2"])
        api.append_log(conn, sub_ingest,
                       f"re-ingested {len(events_v2)} events from upstream_fixed.json")
        api.complete_step(conn, sub_ingest)

        api.start_step(conn, sub_cluster)
        out_v2, holder["purity_v2"] = run_cluster_job(HERE / "upstream_fixed.json")
        api.append_log(conn, sub_cluster, out_v2)
        api.complete_step(conn, sub_cluster)

    results = run_data_decision_loop(
        conn, project=PROJECT, plan_id=plan_id, step_id=s_verify,
        prev_profile=intent_rows(), curr_profile=actual_v1,
        downstream_consumers=["cluster_and_eval.build_features"],
        task_goal="6-way category segmentation of the shelf-monitor feed",
        decide=decide, apply_action=apply_action,
        reprofile=lambda: holder["actual_v2"],
    )

    # ── v2: verified ─────────────────────────────────────────────────────────
    print(bold("\n── v2 · re-verify against the restored feed ──"))
    drifts_v2 = contract_panel(holder["actual_v2"])
    api.append_log(conn, s_verify, "contract re-verified after upstream fix")
    api.complete_step(conn, s_verify)
    api.complete_plan(conn, plan_id)
    purity_v2 = holder["purity_v2"]

    # What got remembered (the never-repeat-a-mistake trail).
    dec = conn.execute("SELECT drift_kind, action, outcome FROM llm_decisions "
                       "WHERE plan_id = ?", (plan_id,)).fetchone()
    ledger = conn.execute(
        "SELECT COUNT(*) FROM LedgerEntries WHERE plan_id = ?", (plan_id,)).fetchone()

    print(bold("\n═══ outcome ═══"))
    print(f"  purity   {red(f'{purity_v1:.2f} (collapsed)')}  →  "
          f"{green(f'{purity_v2:.2f} (verified)')}")
    print(f"  decision recorded: {dec[0]} → {dec[1]} → {dec[2]}"
          f"  ·  ledger entries: {ledger[0]} (anti-pattern remembered)")
    print(dim(f"\n  replay it in the dashboard (read-only):\n"
              f"    ORCH_DB={DB_PATH} bash orchestrator-webapp/launch_dashboard.sh\n"))

    # ── self-check: the demo must reproduce or fail loudly ───────────────────
    ok = (drifts_v1 and not drifts_v2
          and results and results[0]["outcome"] == "resolved"
          and purity_v1 < 0.55 and purity_v2 > 0.85
          and dec is not None and ledger[0] >= 1)
    plan_row = conn.execute("SELECT status FROM Plans WHERE plan_id = ?",
                            (plan_id,)).fetchone()
    ok = ok and plan_row[0] == "COMPLETED"
    conn.close()
    print(green("  SELF-CHECK OK") if ok else red("  SELF-CHECK FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
