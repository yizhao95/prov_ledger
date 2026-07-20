#!/usr/bin/env python3
"""One-command demo: the phantom uplift — a revenue jump that isn't real.

The upstream checkout service ships v2 and silently stops sending
`promo_discount`. The weekly revenue rollup does `order.get("promo_discount",
0.0)` — so every discount becomes $0, net revenue jumps ~+24% week-over-week,
the job exits 0 and its unit tests stay green. It's the only failure class
where the metric moves in the direction everyone WANTS: nobody investigates
good news. The catch has to come from the data contract layer.

A realistic 5-step plan, executed for real against the orchestrator backbone
(every state change through the API — plan, steps, logs, profiles, decisions):

  A ANALYSIS       explore the checkout feed (profile columns/dtypes/nulls)
  B DOCUMENTATION  write the data contract (declared_schema.json artifact)
  C COMMAND        ingest the feed + capture the runtime profile
  D COMMAND        run the revenue rollup + its own pytest suite — all green
  E ANALYSIS       verify the contract: Intent vs Actual -> FAILS
                   (column_dropped: `promo_discount`) -> LLM decision recorded
                   -> deviation sub-steps re-ingest the fixed feed -> VERIFIED

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

PROJECT = "phantom-uplift"
DATASET = "checkout_orders"
DB_PATH = HERE / "demo-orchestrator.db"
BASELINE = HERE / "last_week_metrics.json"
CONTRACT_PATH = HERE / "declared_schema.json"

# The data Intent: what the revenue rollup REQUIRES of the checkout feed.
# Step B writes this out as the contract artifact; step E verifies against it.
DECLARED_SCHEMA = {
    "order_id": "int", "ts": "str", "store_id": "str", "sku": "str",
    "qty": "int", "unit_price": "float", "promo_discount": "float",
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
    lines = [f"{'column':<15} {'dtype':<8} {'null%':>6} {'distinct':>9}"]
    for r in sorted(rows, key=lambda r: r["column_name"]):
        lines.append(f"{r['column_name']:<15} {r['dtype'] or '?':<8} "
                     f"{100 * (r['null_frac'] or 0):>5.0f}% {r['distinct_count']:>9}")
    return "\n".join(lines)


def run_rollup_job(source: Path) -> tuple[str, float, float]:
    """Run the (provLedger-unaware) business job; returns
    (stdout, mean_net, delta_pct_vs_last_week)."""
    proc = subprocess.run(
        [sys.executable, str(HERE / "revenue_rollup.py"), str(source),
         str(BASELINE)],
        capture_output=True, text=True, check=True,
    )
    result = next(line for line in proc.stdout.splitlines()
                  if line.startswith("RESULT"))
    fields = dict(kv.split("=") for kv in result.split()[1:])
    return proc.stdout, float(fields["mean_net"]), float(fields["delta_pct"])


def run_rollup_tests() -> tuple[str, bool]:
    """Run the rollup's OWN pytest suite (the green lights that bless the bug).
    Returns (tail line like '6 passed in 0.02s', all_green)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--no-header",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=HERE,
    )
    tail = [l for l in proc.stdout.strip().splitlines() if l.strip()][-1]
    return tail.strip(), proc.returncode == 0


def contract_panel(actual: list[dict]) -> list[dict]:
    """Print the Intent | Actual panel; returns the drifts (empty = verified)."""
    drifts = detect_drift(intent_rows(), actual)
    bad_cols = {d["column"] for d in drifts}
    actual_by_col = {r["column_name"]: r for r in actual}

    verdict = (red("🔴 MISMATCH") if drifts else green("✅ VERIFIED"))
    print(f"\n  ┌─ data contract · {DATASET} " + "─" * 30)
    print(f"  │ {'column':<15} {'Intent':<10} {'Actual':<14} ")
    print(f"  │ {'-'*15} {'-'*10} {'-'*14}")
    for col, want in DECLARED_SCHEMA.items():
        a = actual_by_col.get(col)
        got = a["dtype"] if a else "— missing —"
        line = f"  │ {col:<15} {want:<10} {got:<14}"
        print(red(line + "  ← required, absent from feed") if col in bad_cols
              else line + dim("  ok"))
    print(f"  │")
    print(f"  │ verdict: {verdict}"
          + (f"  ({', '.join(d['kind'] + ':' + d['column'] for d in drifts)})"
             if drifts else ""))
    print(f"  └" + "─" * 57)
    return drifts


def main() -> int:
    print(bold("\n═══ provLedger demo — the phantom uplift ═══\n"))

    # Fresh, throwaway DB + freshly generated deterministic data (seed 42).
    DB_PATH.unlink(missing_ok=True)
    subprocess.run([sys.executable, str(HERE / "gen_upstream.py")],
                   check=True, capture_output=True)
    baseline_net = json.loads(BASELINE.read_text())["mean_net_revenue"]
    conn = odb.open_db(DB_PATH)
    odb.run_migrations(conn)

    plan = api.initialize_plan(
        conn,
        "Ship the weekly promo-revenue rollup for the merch dashboard",
        [{"description": "Explore the checkout-orders feed: profile columns, "
                         "dtypes, null rates, cardinalities", "step_type": "ANALYSIS"},
         {"description": "Write the data contract: required columns + dtypes "
                         "the revenue rollup depends on", "step_type": "DOCUMENTATION"},
         {"description": "Ingest checkout orders and capture the runtime data "
                         "profile", "step_type": "COMMAND"},
         {"description": "Run the revenue rollup + its unit tests; report "
                         "net revenue vs last week", "step_type": "COMMAND"},
         {"description": "Verify the data contract (Intent vs Actual) and sign "
                         "off the weekly number", "step_type": "ANALYSIS"}],
        user_query="Compute this week's net revenue for the merch dashboard "
                   "and report the week-over-week change.",
    )
    plan_id = plan["plan_id"]
    s_explore, s_contract, s_ingest, s_rollup, s_verify = plan["step_ids"]
    print(f"  plan published: {plan_id} — 5 steps {yellow('PENDING')}")
    _beat(2.5)  # hold the freshly-published PENDING plan (recording opens here)

    # ── A · explore ──────────────────────────────────────────────────────────
    api.start_step(conn, s_explore)
    _beat(1.2)
    orders_v1 = json.loads((HERE / "orders_drifted.json").read_text())
    explore_rows = profile_records(orders_v1, dataset=DATASET)
    api.append_log(conn, s_explore,
                   f"profiled orders_drifted.json: {len(orders_v1)} orders, "
                   f"{len(explore_rows)} columns\n" + profile_summary(explore_rows))
    api.complete_step(conn, s_explore)
    print(f"  A explore   {green('COMPLETED')}  {len(orders_v1)} orders, "
          f"{len(explore_rows)} columns profiled")

    # ── B · contract ─────────────────────────────────────────────────────────
    api.start_step(conn, s_contract)
    _beat(1.0)
    CONTRACT_PATH.write_text(json.dumps(DECLARED_SCHEMA, indent=1) + "\n",
                             encoding="utf-8")
    api.append_log(conn, s_contract,
                   "declared_schema.json written: 7 required columns "
                   f"({', '.join(DECLARED_SCHEMA)}). Downstream "
                   "revenue_rollup.rollup depends on `promo_discount` for the "
                   "net-revenue number the merch dashboard reports.")
    api.complete_step(conn, s_contract)
    print(f"  B contract  {green('COMPLETED')}  declared_schema.json "
          f"({len(DECLARED_SCHEMA)} required columns)")

    # ── C · ingest (the false green begins) ──────────────────────────────────
    api.start_step(conn, s_ingest)
    _beat(1.0)
    actual_v1 = profile_records(orders_v1, dataset=DATASET, project=PROJECT,
                                plan_id=plan_id, step_id=s_ingest)
    odb.insert_data_profile(conn, actual_v1)
    api.append_log(conn, s_ingest,
                   f"ingested {len(orders_v1)} orders from orders_drifted.json; "
                   f"runtime profile captured ({len(actual_v1)} columns) into "
                   "data_profile")
    api.complete_step(conn, s_ingest)
    print(f"  C ingest    {green('COMPLETED')}  runtime profile captured")

    # ── D · rollup (exit 0, tests green — the false good news) ───────────────
    api.start_step(conn, s_rollup)
    _beat(0.8)
    out_v1, net_v1, delta_v1 = run_rollup_job(HERE / "orders_drifted.json")
    pytest_tail, tests_green = run_rollup_tests()
    api.append_log(conn, s_rollup,
                   out_v1 + f"\nunit tests: {pytest_tail}")
    api.complete_step(conn, s_rollup)
    print(f"  D rollup    {green('COMPLETED')}  mean net revenue "
          f"${net_v1:.2f}/order ({delta_v1:+.1f}% vs last week). exit_code=0")
    print(f"              pytest: {green(pytest_tail)}")
    print(dim(f"              (discounts applied: $0.00 — a {delta_v1:+.1f}% "
              "uplift everyone is happy to accept)"))
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
                  reason="data contract MISMATCH (column_dropped: "
                         "`promo_discount`) — discounts are being imputed to "
                         f"$0, so the {delta_v1:+.1f}% revenue uplift is "
                         "phantom. Do NOT ship this number.")
    print(f"  E verify    {red('FAILED')}  (contract MISMATCH recorded)")
    _beat(3.0)

    # ── the decision loop: record -> revise through the backbone -> re-verify ─
    print(bold("\n── revise · LLM decision, recorded + applied through the backbone ──"))
    holder: dict = {}

    def decide(ctx: dict) -> dict:
        d = ctx["drift"]
        return {
            "action": "coerce_upstream",
            "decision": f"Checkout-service v2 silently dropped `{d['column']}` "
                        "— restore the field upstream, then re-ingest and "
                        "re-run the rollup before any number ships.",
            "rationale": "revenue_rollup imputes promo_discount to $0 via "
                         ".get(col, 0.0); the job exits 0 and its tests pass, "
                         "but net revenue inflates by exactly the discount "
                         "total. Fix the source, don't patch downstream.",
        }

    def apply_action(action: str, d: dict) -> None:
        # ALL state changes go through the backbone: a recorded deviation that
        # inserts sub-steps, then normal step execution. Nothing edits state.
        dev = api.evaluate_and_update_plan(
            conn, deviation_detected=True, target_step_id=s_verify,
            justification=f"data contract MISMATCH ({d['kind']}: `{d['column']}`): "
                          "checkout v2 dropped the promo field; restoring it "
                          "and re-running before the number ships.",
            new_sub_steps=[{"description": "Re-ingest orders from the restored "
                                           "checkout feed", "step_type": "COMMAND"},
                           {"description": "Re-run the revenue rollup on the "
                                           "restored feed", "step_type": "COMMAND"}])
        assert dev["accepted"], dev
        sub_ingest, sub_rollup = dev["new_step_ids"]
        print(f"  deviation recorded (rev {dev['revision_count']}) "
              f"→ sub-steps {sub_ingest.split('-')[-1]}, {sub_rollup.split('-')[-1]}")

        api.start_step(conn, sub_ingest)
        _beat(1.2)
        orders_v2 = json.loads((HERE / "orders_fixed.json").read_text())
        holder["actual_v2"] = profile_records(orders_v2, dataset=DATASET,
                                              project=PROJECT, plan_id=plan_id,
                                              step_id=sub_ingest)
        odb.insert_data_profile(conn, holder["actual_v2"])
        api.append_log(conn, sub_ingest,
                       f"re-ingested {len(orders_v2)} orders from orders_fixed.json")
        api.complete_step(conn, sub_ingest)

        api.start_step(conn, sub_rollup)
        _beat(1.0)
        out_v2, holder["net_v2"], holder["delta_v2"] = \
            run_rollup_job(HERE / "orders_fixed.json")
        api.append_log(conn, sub_rollup, out_v2)
        api.complete_step(conn, sub_rollup)

    results = run_data_decision_loop(
        conn, project=PROJECT, plan_id=plan_id, step_id=s_verify,
        prev_profile=intent_rows(), curr_profile=actual_v1,
        downstream_consumers=["revenue_rollup.rollup"],
        task_goal="weekly net-revenue rollup for the merch dashboard",
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
    net_v2, delta_v2 = holder["net_v2"], holder["delta_v2"]

    # What got remembered (the never-repeat-a-mistake trail).
    dec = conn.execute("SELECT drift_kind, action, outcome FROM llm_decisions "
                       "WHERE plan_id = ?", (plan_id,)).fetchone()
    ledger = conn.execute(
        "SELECT COUNT(*) FROM LedgerEntries WHERE plan_id = ?", (plan_id,)).fetchone()

    _beat(2.0)
    print(bold("\n═══ outcome ═══"))
    print(f"  mean net revenue / order   last week ${baseline_net:.2f}")
    print(f"    {red(f'${net_v1:.2f} ({delta_v1:+.1f}% — phantom, discounts imputed to $0)')}")
    print(f"    {green(f'${net_v2:.2f} ({delta_v2:+.1f}% — verified against the contract)')}")
    print(f"  decision recorded: {dec[0]} → {dec[1]} → {dec[2]}"
          f"  ·  ledger entries: {ledger[0]} (anti-pattern remembered)")
    print(dim("\n  replay it in the dashboard (read-only), from the repo root:\n"
              f"    ORCH_DB=$PWD/{DB_PATH.relative_to(REPO)} "
              "bash orchestrator-webapp/launch_dashboard.sh\n"))

    # ── self-check: the demo must reproduce or fail loudly ───────────────────
    phantom_ratio = net_v1 / net_v2
    ok = (drifts_v1 and not drifts_v2
          and any(d["kind"] == "column_dropped" and d["column"] == "promo_discount"
                  for d in drifts_v1)
          and results and results[0]["outcome"] == "resolved"
          and tests_green and "passed" in pytest_tail
          and 1.15 < phantom_ratio < 1.35   # the uplift is big AND believable
          and abs(delta_v2) < 5.0           # true number ≈ flat week-over-week
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
