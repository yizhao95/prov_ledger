# silent-class-drop — the demo of why provLedger exists

A pipeline that **runs green and is still wrong** — and the data contract that
catches it.

## The story

A shelf-monitoring vision service publishes detection events (one JSON record
per detected item box), enriched with a `class` field — the product category,
joined in from a catalog service. A downstream job clusters the events into 6
category segments; the business metric is segment **purity** against a
manual-audit ground truth.

One day an upstream refactor **silently drops the `class` enrichment**. The
downstream job doesn't crash — KMeans happily forms 6 clusters from geometry
alone and exits 0. Every step goes green. Only the purity has collapsed:

| upstream feed | class present? | contract verdict | segment purity |
|---|---|---|---|
| `upstream_drifted.json` | no  | 🔴 MISMATCH (`column_dropped:class`) | **0.31** (collapsed; 1/6 ≈ 0.17 floor) |
| `upstream_fixed.json`   | yes | ✅ VERIFIED | **0.91** (meaningful) |

The demo runs the whole arc against the real orchestrator backbone:

1. **v1** — ingest the drifted feed, cluster: `6 clusters formed. exit_code=0`,
   steps COMPLETED. The false success.
2. **Catch** — the verify step compares the declared data **Intent**
   (`box_id, xmin, xmax, ymin, ymax, class`) against the runtime **Actual**
   profile → **MISMATCH**, `class` is missing.
3. **Revise** — the decision loop records the LLM decision (and the
   anti-pattern in the ledger, so it's never repeated), then revises the plan
   through the backbone: a recorded deviation inserts sub-steps. Nothing ever
   edits state by hand.
4. **v2** — re-ingest the fixed feed → contract **VERIFIED**, purity 0.31 → 0.91.

## Run it

From the repo root (creates a local venv, installs the two pinned deps):

```bash
make demo
```

Or manually, with `numpy` + `scikit-learn` installed:

```bash
python examples/silent-class-drop/run_demo.py
```

Everything lands in `examples/silent-class-drop/demo-orchestrator.db` — a
throwaway DB, never your real orchestrator DB. Replay it in the read-only
dashboard:

```bash
ORCH_DB=$PWD/examples/silent-class-drop/demo-orchestrator.db \
  bash orchestrator-webapp/launch_dashboard.sh
```

The plan view shows the step tree, the revision history (why the plan
changed), and the **📊 Data** panel: the latest profile snapshot per dataset
plus the drift → decision → outcome trail.

## Files

- `gen_upstream.py` — deterministic synthetic generator (seed 42, stdlib-only).
  Emits `upstream_fixed.json`, `upstream_drifted.json` (same events, `class`
  removed) and `ground_truth.csv`. ~10% of feed `class` values are deliberate
  catalog-join errors, so even the healthy feed tops out at 0.91, not 1.00.
- `cluster_and_eval.py` — the downstream business job (provLedger-unaware):
  features → KMeans(6) → purity. Exits 0 in both worlds — that's the point.
- `run_demo.py` — the orchestration: plan + steps, Intent-vs-Actual contract
  check, decision loop, deviation, re-run, self-check. Exits non-zero if the
  arc doesn't reproduce.

Data is **synthetic and illustrative**; the failure mechanism it reproduces —
an upstream column silently disappearing while everything stays green — is
real-world-derived. All numbers above are deterministic (seed 42, pinned deps).

## Regenerating the media

`docs/media/silent-class-drop.gif` (needs [vhs](https://github.com/charmbracelet/vhs),
ttyd and ffmpeg on PATH; ~20s, <1MB):

```bash
vhs examples/silent-class-drop/demo.tape        # from the repo root
```

`docs/media/dashboard-task.png` — run the demo, launch the dashboard against
`demo-orchestrator.db` (command above), open the plan view, expand the
📊 Data + Revision history panels, screenshot at 1440px wide.

`docs/media/dataflow.png` — build the project-state-graph over the two
business files and render the dataflow slice:

```bash
mkdir -p /tmp/scd-pipeline
cp examples/silent-class-drop/{gen_upstream.py,cluster_and_eval.py} /tmp/scd-pipeline/
cd skills/project-state-graph/scripts
python -m analyzer /tmp/scd-pipeline --project silent-class-drop --db-path /tmp/scd.db
python viz_slices.py /tmp/scd.db /tmp/scd-slices.html --title "silent-class-drop · ingest → cluster"
# open /tmp/scd-slices.html, ① Dataflow + datatype tab, screenshot
```
The image is labeled "dashboard view WIP" because the dataflow view is rendered
from the project-state-graph, not yet embedded in the dashboard.
