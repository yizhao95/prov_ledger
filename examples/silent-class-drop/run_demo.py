#!/usr/bin/env python3
"""One-command demo: a silent upstream class-drop, caught by the data contract.

A realistic 5-step plan, executed for real against the orchestrator backbone
(every state change through the API — plan, steps, logs, profiles, decisions):

  A ANALYSIS       explore the upstream feed (profile columns/dtypes/nulls)
  B DOCUMENTATION  write the data contract (declared_schema.json artifact)
  C COMMAND        ingest the feed + capture the runtime profile
  D COMMAND        cluster into 6 category segments, evaluate purity — exit 0
  E ANALYSIS       verify the contract: Intent vs Actual -> FAILS
                   (column_dropped: `class`) -> LLM decision recorded ->
                   deviation sub-steps re-ingest the fixed feed -> VERIFIED

Everything lands in ./demo-orchestrator.db (NEVER your real orchestrator DB),
so the read-only dashboard can replay the whole story afterwards.

Usage:  python run_demo.py           (or `make demo` from the repo root)
DEMO_PACE=<seconds> stretches the acts so the dashboard's 2s poll can watch
the run live (used for recording; defaults to 0 — tests/CI run at full speed).
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
CONTRACT_PATH = HERE / "declared_schema.json"

# The data Intent: what the downstream clustering step REQUIRES of the feed.
# Step B writes this out as the contract artifact; step E verifies against it.
DECLARED_SCHEMA = {
    "box_id": "int", "xmin": "float", "xmax": "float",
    "ymin": "float", "ymax": "float", "class": "str",
}

# DEMO_PACE (seconds, default 0) inserts a beat between/inside the demo's acts —
# used only when recording; tests and CI run with no pauses.
_PACE = float(os.environ.get("DEMO_PACE", "0") or 0)
def _beat(mult: float = 1.0) -> None:
    if _PACE:
        import time
        time.sleep(_PACE * mult)


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
    """The declared Intent (from the contract artifact), shaped like profile
    rows so drift-detection can compare it directly against the runtime Actual."""
    declared = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return [{"dataset": DATASET, "column_name": col, "dtype": dtype,
             "null_frac": 0.0} for col, dtype in declared.items()]


def profile_summary(rows: list[dict]) -> str:
    """Human-readable table of a runtime profile — the explore step's log."""
    lines = [f"{'column':<10} {'dtype':<8} {'null%':>6} {'distinct':>9}"]
    for r in sorted(rows, key=lambda r: r["column_name"]):
        lines.append(f"{r['column_name']:<10} {r['dtype'] or '?':<8} "
                     f"{100 * (r['null_frac'] or 0):>5.0f}% {r['distinct_count']:>9}")
    return "\n".join(lines)


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
        [{"description": "Explore the upstream feed: profile columns, dtypes, "
                         "null rates, cardinalities", "step_type": "ANALYSIS"},
         {"description": "Write the data contract: required columns + dtypes "
                         "the clustering step depends on", "step_type": "DOCUMENTATION"},
         {"description": "Ingest upstream detection events and capture the "
                         "runtime data profile", "step_type": "COMMAND"},
         {"description": "Cluster events into 6 category segments and evaluate "
                         "purity", "step_type": "COMMAND"},
         {"description": "Verify the data contract (Intent vs Actual) and sign "
                         "off", "step_type": "ANALYSIS"}],
        user_query="Segment the shelf-monitor detection feed into 6 "
                   "product-category groups and report segment purity.",
    )
    plan_id = plan["plan_id"]
    s_explore, s_contract, s_ingest, s_cluster, s_verify = plan["step_ids"]
    print(f"  plan published: {plan_id} — 5 steps {yellow('PENDING')}")
    _beat(2.5)  # hold the freshly-published PENDING plan (recording opens here)

    # ── A · explore ──────────────────────────────────────────────────────────
    api.start_step(conn, s_explore)
    _beat(1.2)
    events_v1 = json.loads((HERE / "upstream_drifted.json").read_text())
    explore_rows = profile_records(events_v1, dataset=DATASET)
    api.append_log(conn, s_explore,
                   f"profiled upstream_drifted.json: {len(events_v1)} events, "
                   f"{len(explore_rows)} columns\n" + profile_summary(explore_rows))
    api.complete_step(conn, s_explore)
    print(f"  A explore   {green('COMPLETED')}  {len(events_v1)} events, "
          f"{len(explore_rows)} columns profiled")

    # ── B · contract ─────────────────────────────────────────────────────────
    api.start_step(conn, s_contract)
    _beat(1.0)
    CONTRACT_PATH.write_text(json.dumps(DECLARED_SCHEMA, indent=1) + "\n",
                             encoding="utf-8")
    api.append_log(conn, s_contract,
                   "declared_schema.json written: 6 required columns "
                   f"({', '.join(DECLARED_SCHEMA)}). Downstream "
                   "cluster_and_eval.build_features depends on the `class` "
                   "enrichment for category segmentation.")
    api.complete_step(conn, s_contract)
    print(f"  B contract  {green('COMPLETED')}  declared_schema.json "
          f"({len(DECLARED_SCHEMA)} required columns)")

    # ── C · ingest (the false green begins) ──────────────────────────────────
    api.start_step(conn, s_ingest)
    _beat(1.0)
    actual_v1 = profile_records(events_v1, dataset=DATASET, project=PROJECT,
                                plan_id=plan_id, step_id=s_ingest)
    odb.insert_data_profile(conn, actual_v1)
    api.append_log(conn, s_ingest,
                   f"ingested {len(events_v1)} events from upstream_drifted.json; "
                   f"runtime profile captured ({len(actual_v1)} columns) into "
                   "data_profile")
    api.complete_step(conn, s_ingest)
    print(f"  C ingest    {green('COMPLETED')}  runtime profile captured")

    # ── D · cluster (exit 0 — the false success) ─────────────────────────────
    api.start_step(conn, s_cluster)
    _beat(0.8)
    out_v1, purity_v1 = run_cluster_job(HERE / "upstream_drifted.json")
    api.append_log(conn, s_cluster, out_v1)
    api.complete_step(conn, s_cluster)
    print(f"  D cluster   {green('COMPLETED')}  6 clusters formed. exit_code=0")
    print(dim(f"              (eval harness: segment purity {purity_v1:.2f} "
              f"— nobody is looking at this yet)"))
    _beat(1.5)

    # ── E · verify: Intent vs Actual — the catch ─────────────────────────────
    print(bold("\n── verify · declared Intent vs runtime Actual ──"))
    api.start_step(conn, s_verify)
    _beat(0.8)
    drifts_v1 = contract_panel(actual_v1)
    assert drifts_v1, "demo invariant: the drifted feed must MISMATCH"
    # The failure is FIRST-CLASS: the verify step FAILS with the contract
    # verdict as its reason (red on the dashboard, never hidden). Recovery
    # happens through sub-steps under it — the state machine's designed path.
    api.fail_step(conn, s_verify,
                  reason="data contract MISMATCH (column_dropped: `class`) — "
                         "upstream feed is missing a required enrichment column")
    print(f"  E verify    {red('FAILED')}  (contract MISMATCH recorded)")
    _beat(3.0)

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
            new_sub_steps=[{"description": "Re-ingest events from the fixed upstream feed",
                            "step_type": "COMMAND"},
                           {"description": "Re-run clustering on the restored feed",
                            "step_type": "COMMAND"}])
        assert dev["accepted"], dev
        sub_ingest, sub_cluster = dev["new_step_ids"]
        print(f"  deviation recorded (rev {dev['revision_count']}) "
              f"→ sub-steps {sub_ingest.split('-')[-1]}, {sub_cluster.split('-')[-1]}")

        api.start_step(conn, sub_ingest)
        _beat(1.2)
        events_v2 = json.loads((HERE / "upstream_fixed.json").read_text())
        holder["actual_v2"] = profile_records(events_v2, dataset=DATASET,
                                              project=PROJECT, plan_id=plan_id,
                                              step_id=sub_ingest)
        odb.insert_data_profile(conn, holder["actual_v2"])
        api.append_log(conn, sub_ingest,
                       f"re-ingested {len(events_v2)} events from upstream_fixed.json")
        api.complete_step(conn, sub_ingest)

        api.start_step(conn, sub_cluster)
        _beat(1.0)
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
    _beat(1.5)
    drifts_v2 = contract_panel(holder["actual_v2"])
    # s_verify stays FAILED (terminal); its completed sub-steps are the
    # recovery, which is exactly how the plan-level rollup reads it.
    api.append_log(conn, s_verify, "contract re-verified after upstream fix "
                                   "(recovered via sub-steps)")
    api.complete_plan(conn, plan_id)
    purity_v2 = holder["purity_v2"]

    # What got remembered (the never-repeat-a-mistake trail).
    dec = conn.execute("SELECT drift_kind, action, outcome FROM llm_decisions "
                       "WHERE plan_id = ?", (plan_id,)).fetchone()
    ledger = conn.execute(
        "SELECT COUNT(*) FROM LedgerEntries WHERE plan_id = ?", (plan_id,)).fetchone()

    _beat(2.0)
    print(bold("\n═══ outcome ═══"))
    print(f"  purity   {red(f'{purity_v1:.2f} (collapsed)')}  →  "
          f"{green(f'{purity_v2:.2f} (verified)')}")
    print(f"  decision recorded: {dec[0]} → {dec[1]} → {dec[2]}"
          f"  ·  ledger entries: {ledger[0]} (anti-pattern remembered)")
    print(dim("\n  replay it in the dashboard (read-only), from the repo root:\n"
              f"    ORCH_DB=$PWD/{DB_PATH.relative_to(REPO)} "
              "bash orchestrator-webapp/launch_dashboard.sh\n"))

    # ── self-check: the demo must reproduce or fail loudly ───────────────────
    ok = (drifts_v1 and not drifts_v2
          and results and results[0]["outcome"] == "resolved"
          and purity_v1 < 0.55 and purity_v2 > 0.85
          and dec is not None and ledger[0] >= 1)
    plan_row = conn.execute("SELECT status FROM Plans WHERE plan_id = ?",
                            (plan_id,)).fetchone()
    ok = ok and plan_row[0] == "COMPLETED"
    vrow = conn.execute("SELECT status, failure_reason FROM Steps WHERE step_id = ?",
                        (s_verify,)).fetchone()
    ok = ok and vrow[0] == "FAILED" and "MISMATCH" in (vrow[1] or "")
    types = [r[0] for r in conn.execute(
        "SELECT step_type FROM Steps WHERE plan_id = ? AND parent_step_id IS NULL "
        "ORDER BY execution_order", (plan_id,))]
    ok = ok and types == ["ANALYSIS", "DOCUMENTATION", "COMMAND", "COMMAND", "ANALYSIS"]
    conn.close()
    print(green("  SELF-CHECK OK") if ok else red("  SELF-CHECK FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
