# Outcomes — what a plan's claims turned into

provLedger records, at the moment a plan is published or executed, what the
plan **expects** of the data and code it touches, and later records what
**actually happened** to each expectation. The expectation is a claim; the
outcome is a measurement. Nothing in this path is written by a model: a
model may *state* a claim, but every outcome value is computed from rows
that were recorded by a run, a profile, a metric or the state graph.

```
expectation  (plan P1 says: "promo_discount stays present", channel profile_drift)
      │
      │   another plan of the same project closes reviewed (P2)
      ▼
outcome      (kind observed, source data_profile, value {drifts: [column_dropped promo_discount]},
              backfilled_by_plan P2)
```

## 1 · Expectations

`expectations` (migration 014) — append-only.

| column | meaning |
|---|---|
| `plan_id`, `step_id` | who made the claim |
| `project` | the registered project the claim belongs to |
| `target`, `target_kind` | what it is about: a `dataset`, a `column`, a code `node`, or a `metric` |
| `claim` | the sentence, verbatim |
| `channel` | **how** it can be observed — see §3 |
| `created_at` | the claim's timestamp; outcomes compare *before* this against *after* it |

Write one with `db.insert_expectation(...)` (the phantom-uplift demo does), or
through a plan's close-time hooks. A claim with `channel = "none"` is legal
and honest: "there is no way to observe this yet".

## 2 · Outcomes

`outcomes` (migration 014) — append-only, one or more rows per expectation.

| column | meaning |
|---|---|
| `kind` | `observed` (a channel measured it) · `survival` (derived from the state graph: did the node/column survive, who churned it) · `none_available` (nothing to observe, with the reason) |
| `value_json` | the measurement (shape depends on the channel, §3) |
| `source` | which channel / table produced it: `data_profile`, `metrics`, `state_graph`, a vendor id, or `none` |
| `tier` | `observed` · `derived` · `none` — the evidence tier; never `asserted`, outcomes are not opinions |
| `reason` | for `none_available`: why (which side was missing, which channels were asked) |
| `backfilled_by_plan` | the closing plan that triggered the backfill |

### When outcomes are written

`outcomes.backfill` runs inside `api.review_and_complete` when a
registered-project plan closes **reviewed**. It judges the expectations of
**other** plans of the project:

- `observed` expectations are judged **once**: the first close after the
  claim that can see both sides writes an `observed` or a `none_available`
  row and the expectation is settled.
- `survival` (node / column / `graph` channel) is **re-judged at every
  close** and a row is appended only when the verdict changed (a node
  churned by three plans is news after two).

A failure inside a channel never blocks the close: it becomes a
`none_available` row with the error in `reason`.

## 3 · Channels

A channel is an `OutcomeChannel` (`provledger.outcome_channels`): it says
whether it applies to an expectation (`applicable_to`) and measures it
(`collect`). Built-ins:

### `profile_drift` — the data profile before vs after

Target: a dataset that `data_profile` rows describe (written by
`orchestrator.profiler.profile_records` + `db.insert_data_profile`). The
channel takes the latest snapshot **at or before** `created_at` and the
latest **after** it and runs `drift.detect_drift` (with the project's
declared drift kinds):

```json
{"drifts": [{"kind": "column_dropped", "column": "promo_discount", "source": "builtin"}], "kinds": ["column_dropped"]}
```

Missing a side → `none_available`, reason `no data_profile snapshot before and after the expectation`.

### `metric:<name>` — a number before vs after

Target: a metric recorded in `metrics` (migration 017) under the project.
Record one:

```bash
# executing-plans, from a step that measured it:
bash skills/executing-plans/scripts/record-metric.sh in.json      # {"step_id": "...", "name": "mean_net_revenue", "value": 64.33, "unit": "usd"}
# or let run-step.sh pick it out of the command's stdout:
#   {"step_id": "...", "type": "COMMAND", "command": "python rollup.py", "metrics_from_stdout": true}
#   ... where the command prints:  metric name=mean_net_revenue value=64.33 unit=usd
```

`value` must be a finite number — `record-metric` refuses `"high"`, `null`,
`true`, `"nan"` and writes nothing; the table's CHECK refuses anything that
is not a real. The channel takes the nearest metric row **at or before**
`created_at` and the nearest **after** it:

```json
{"name": "mean_net_revenue", "before": 52.22, "after": 64.33, "delta": 12.11, "delta_pct": 23.19,
 "unit": "usd", "before_at": "2026-09-11 00:30:00", "after_at": "2026-09-11 02:00:00",
 "before_plan": "PU1", "after_plan": "PU2"}
```

A zero baseline gives `delta_pct: null`. Missing a side → `none_available`,
reason `no metric 'mean_net_revenue' observed after the expectation (...)`.

This is the phantom-uplift story told as data: plan 1 claims *"revenue stays
within ±5% WoW"*; the feed silently loses `promo_discount`; every discount
imputes to $0; the rollup reports **+23.2%** and its tests pass. The number
got *better*, so nobody looked — and the close of the next plan writes the
+23.2% as the observed outcome of the ±5% claim. The claim is contradicted
by a measurement, on the record, without anyone judging it.

### `none` / unknown

`channel = "none"` → `none_available` with the claim as the reason. A channel
string no channel recognises → `none_available` whose reason lists the
channels that were available.

### Third-party channels

Declare them in `provledger-extensions.json` (`outcome_channels`, see
[`docs/extensions.md`](extensions.md) §8). Channels are asked highest
priority first; the first non-`None` answer wins; a channel that raises is
isolated and the error is kept on the winning value under `channel_errors`.

## 4 · Reading outcomes

- Dashboard: the plan card's **🎯 Outcomes** panel (read-only) — the claim,
  the channel, the tier badge, a one-line summary (`52.22 usd → 64.33 usd
  (+23.2%)`, `drift: column_dropped`, the `none_available` reason).
- SQL, the line a reviewer pastes into a PR:

```sql
SELECT e.plan_id, e.target, e.claim, o.kind, o.source, o.tier, o.value_json, o.reason, o.backfilled_by_plan
FROM outcomes o JOIN expectations e ON e.id = o.expectation_id ORDER BY o.id;
```

## 5 · What is deliberately not here

- No aggregation window for metrics: the channel compares two rows, the
  nearest on each side. A rolling mean or a tolerance band is a later
  channel ([known issues](KNOWN-ISSUES.md)).
- No verdict: an outcome records *what was measured*, never whether the
  claim "held". `delta_pct = 23.19` next to "within ±5%" speaks for itself;
  a pass/fail column would be an opinion.
- No model anywhere in the path. `record-metric` is the only door for a
  number, and it accepts numbers only.
