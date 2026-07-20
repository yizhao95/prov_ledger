# phantom-uplift — the demo of why provLedger exists

A revenue number that **goes up for the wrong reason** — and the data contract
that refuses to sign it off.

## The story

A checkout service publishes one JSON record per order, including
`promo_discount` — the dollars taken off by the promo running this week. The
weekly rollup computes net revenue for the merch dashboard:

```python
discounts = sum(float(o.get("promo_discount", 0.0)) for o in orders)
```

One day the checkout-service **v2 rollout silently stops sending
`promo_discount`**. That `.get(..., 0.0)` — a perfectly reasonable line,
blessed by its own unit test — now imputes $0 for every order. Net revenue
jumps **+23.2% week-over-week**. The job exits 0. `pytest: 6 passed`. And this
is the failure class where the metric moves in the direction everyone *wants*:
nobody investigates good news.

| checkout feed | promo_discount? | contract verdict | mean net revenue / order |
|---|---|---|---|
| `orders_drifted.json` | absent | 🔴 MISMATCH (`column_dropped:promo_discount`) | **$64.33 (+23.2%)** — phantom |
| `orders_fixed.json`   | present | ✅ VERIFIED | **$54.06 (+3.5%)** — real |

The demo publishes and executes a realistic **5-step plan** against the real
orchestrator backbone (every step runs for real, with its own log):

1. **A · ANALYSIS — explore** the checkout feed: profile columns, dtypes,
   null rates, cardinalities (the profile table lands in the step log).
2. **B · DOCUMENTATION — write the data contract**: `declared_schema.json`,
   the 7 required columns the revenue rollup depends on.
3. **C · COMMAND — ingest** the feed and capture the runtime profile into
   `data_profile`.
4. **D · COMMAND — run the rollup**: `exit_code=0`, its own pytest suite
   green, `+23.2% vs last week` — the false good news, COMPLETED.
5. **E · ANALYSIS — verify the contract**: declared **Intent** vs runtime
   **Actual** → **MISMATCH**, `promo_discount` is missing. The step **FAILS**
   with the verdict as its `failure_reason` ("the +23.2% uplift is phantom.
   Do NOT ship this number.") — a red card with the reason banner on the
   dashboard, never hidden, even after the plan recovers.
   The decision loop records the LLM decision (and the anti-pattern in the
   ledger, so it's never repeated), then revises the plan through the
   backbone: a recorded deviation inserts recovery sub-steps that re-ingest
   the fixed feed → contract **VERIFIED**, the real number is +3.5%. Nothing
   ever edits state by hand.

## Run it

From the repo root:

```bash
make demo
```

Or manually (the demo is stdlib-only; `pytest` is used for the green-lights
beat in step D):

```bash
python examples/phantom-uplift/run_demo.py
```

Everything lands in `examples/phantom-uplift/demo-orchestrator.db` — a
throwaway DB, never your real orchestrator DB. Replay it in the read-only
dashboard:

```bash
ORCH_DB=$PWD/examples/phantom-uplift/demo-orchestrator.db \
  bash orchestrator-webapp/launch_dashboard.sh
```

The plan view shows the step tree, the revision history (why the plan
changed), and the **📊 Data** panel: the latest profile snapshot per dataset
plus the drift → decision → outcome trail.

## Files

- `gen_upstream.py` — deterministic synthetic generator (seed 42, stdlib-only).
  Emits `orders_fixed.json`, `orders_drifted.json` (same orders,
  `promo_discount` removed) and `last_week_metrics.json` (the week-over-week
  baseline, same promo running).
- `revenue_rollup.py` — the downstream business job (provLedger-unaware):
  gross − discounts → mean net per order, vs-baseline delta. Exits 0 in both
  worlds — that's the point.
- `tests/test_rollup.py` — the rollup's own unit tests. All green in both
  worlds; one of them (`test_missing_promo_discount_defaults_to_zero`) is the
  bug's alibi — it pins the imputation as correct, which it is *per-record*.
- `run_demo.py` — the orchestration: plan + steps, Intent-vs-Actual contract
  check, decision loop, deviation, re-run, self-check. Exits non-zero if the
  arc doesn't reproduce.

Data is **synthetic and illustrative**; the failure mechanism it reproduces —
an upstream column silently disappearing while every guardrail stays green —
is a reconstruction of a real-world failure *class*, not of any real incident.
All numbers above are deterministic (seed 42, stdlib generator). The size of
the phantom uplift is set by two declared knobs in `gen_upstream.py`
(`PROMO_ATTACH_RATE = 0.50`, `PROMO_DEPTH = (0.22, 0.42)` →
E[uplift] ≈ +19% vs the same week, +23.2% as reported week-over-week); the
demo reports whatever the generated data actually computes to.

## Regenerating the media

`docs/media/phantom-uplift-dashboard.gif` — the README hero: the demo as
the **live dashboard** sees it (opens on the freshly-published PENDING plan →
steps execute live → verify FAILS red with the MISMATCH reason + log →
drift → decision trail → recovered, failure kept visible).
Needs playwright (`python -m playwright install chromium`) + ffmpeg:

```bash
python examples/phantom-uplift/record_dashboard.py   # from the repo root
```

`docs/media/phantom-uplift.gif` — the same arc as a terminal recording
(needs [vhs](https://github.com/charmbracelet/vhs), ttyd and ffmpeg on PATH):

```bash
vhs examples/phantom-uplift/demo.tape        # from the repo root
```

`docs/media/dashboard-task.png` — run the demo, launch the dashboard against
`demo-orchestrator.db` (command above), open the plan view, expand the
📊 Data + Revision history panels, screenshot at 1440px wide.

`docs/media/dataflow.png` — build the project-state-graph over the two
business files and render the dataflow slice:

```bash
mkdir -p /tmp/pu-pipeline
cp examples/phantom-uplift/{gen_upstream.py,revenue_rollup.py} /tmp/pu-pipeline/
cd skills/project-state-graph/scripts
python -m analyzer /tmp/pu-pipeline --project phantom-uplift --db-path /tmp/pu.db
python viz_slices.py /tmp/pu.db /tmp/pu-slices.html --title "phantom-uplift · ingest → rollup"
# open /tmp/pu-slices.html, ① Dataflow + datatype tab, screenshot
```
The image is labeled "dashboard view WIP" because the dataflow view is rendered
from the project-state-graph, not yet embedded in the dashboard.
