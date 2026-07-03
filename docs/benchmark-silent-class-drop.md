# The pipeline was green. The numbers were garbage.

*A tiny, fully-reproducible benchmark of what a data contract catches that
exit codes can't — 0.31 vs 0.91 segment purity on the same "successful"
pipeline.*

---

## The failure class

There's a category of data-pipeline failure that no test suite, no exit code,
and no green CI run will ever show you: **the code is fine, the data broke its
promise.**

A concrete, real-world-derived shape of it: an upstream service refactors and
silently stops emitting one enriched column. Every downstream job still runs.
Nothing raises. KMeans will *always* hand you back 6 clusters if you ask for
6 clusters. `exit_code=0`. Dashboards stay green. The numbers ship — and
they're garbage.

If a coding agent is the one running your pipeline, this gets worse: the agent
sees the green exit code and marks the step COMPLETED. False success is now
*recorded as* success.

## The benchmark

[`examples/silent-class-drop/`](../examples/silent-class-drop/) is a minimal,
offline, deterministic reproduction of that failure — and of the mechanical
catch. Synthetic data, real mechanism (the data is **illustrative**; the
failure mode and the detection path are exactly what runs in production use).

The setup: a shelf-monitoring vision service publishes detection events
(bounding box + a `class` product-category enrichment joined from a catalog
service). A downstream job clusters the events into 6 category segments; the
business metric is segment **purity** against a manual-audit ground truth.

One day upstream drops `class`. The downstream job does not care:

| upstream feed | job result | contract verdict | segment purity |
|---|---|---|---|
| `class` silently dropped | `6 clusters formed. exit_code=0` ✅ | 🔴 **MISMATCH** (`column_dropped:class`) | **0.31** (collapsed; the 6-class floor is ~0.17) |
| `class` restored | `6 clusters formed. exit_code=0` ✅ | ✅ **VERIFIED** | **0.91** |

Same code. Same exit code. Same green steps. **Three×** difference in the
number that matters.

The 0.91 (not 1.00) is deliberate honesty: ~10% of the feed's `class` values
are simulated catalog-join errors, because real enrichments are imperfect too.

## What catches it

Not vigilance — a **contract**, checked mechanically:

1. The plan's ingest step **profiles the data at runtime** (dtype, null
   fraction, row count, distinct count per column) into a `data_profile`
   table.
2. A **declared schema** — the data Intent the downstream step requires
   (`box_id:int, xmin:float, …, class:str`) — is written as an artifact at
   plan time.
3. A verify step diffs **Intent vs Actual** with a pure-stdlib drift detector:
   `column_dropped`, `dtype_changed`, `null_spike`, `cardinality_collapse`.
4. On MISMATCH the verify step **fails loudly** (red, with the verdict as its
   failure reason), the LLM's decision about the drift is **recorded** (and
   ledgered as an anti-pattern so it's never repeated), and the plan revises
   itself through the orchestrator's deviation mechanism — sub-steps re-ingest
   the fixed feed and re-verify.

The point isn't the clustering. The point is that **"exit 0" and "the data
kept its promise" are different claims**, and only one of them was being
checked.

## Reproduce it (2 minutes, offline)

```bash
git clone https://github.com/yizhao95/prov_ledger.git
cd prov_ledger
make demo
```

Deterministic (seed 42, pinned deps): you get **exactly** 0.31 → 0.91, plus a
self-check that fails the run if the arc doesn't reproduce. Then watch the
whole story replay in the read-only dashboard:

```bash
ORCH_DB=$PWD/examples/silent-class-drop/demo-orchestrator.db \
  bash orchestrator-webapp/launch_dashboard.sh
```

![the dashboard replay: steps green, verify FAILED with the contract MISMATCH reason, drift → decision trail, recovery sub-steps](media/silent-class-drop-dashboard.gif)

## Scope, honestly

- The demo data is synthetic and labeled as such; the detection mechanism is
  the real one (runtime profiling + declared-schema drift detection + recorded
  decisions, all in [provLedger](https://github.com/yizhao95/prov_ledger)).
- Purity numbers are specific to this synthetic setup — the benchmark's claim
  is *"green ≠ correct, and the gap is mechanically catchable,"* not any
  particular effect size.
- The same drift detector also flags dtype flips (the classic
  numeric→string label corruption that makes a model predict one class) and
  null spikes; the state-graph side adds a data-leakage gate. Those are
  separate demos-in-waiting.
