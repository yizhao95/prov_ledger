---
name: project-state-graph
description: Use when setting up a new project, creating a new app, kicking off a new initiative, onboarding an existing repo, or whenever you need a repo graph / code graph / project state graph / map of a codebase before working in it. Keywords - set up project, new project, new app, new initiative, project state graph, repo graph, code graph, codebase map, architecture overview.
---

# Project State Graph

## Overview

Initialize a complete, two-layer **state graph** for ANY repo with one command, so future work has an accurate map of the codebase before touching it.

- **Shallow layer** — `ARCHITECTURE.md`: human-readable overview, file list, subsystem grouping, pointers into the deep layer.
- **Deep layer** — `<name>-state-graph.db`: a SQLite graph (nodes, edges, `data_var`, `consistency_card`, `symbol_card`) for precise impact analysis.

A global **registry** (`projects.json` + `PROJECT-STATE-GRAPHS.md` index) tracks every project graph on record.

**Phase A (provLedger) — stored assumed-schema:** when code reads an external
source, the analyzer records the columns/keys the code *expects* it to return as
`metadata_json["assumed_schema"]` on `sql_table` / `bq_dataset` nodes (SQL SELECT
projection) and `api_source` nodes (response subscript keys). This feeds the
reviewer's contract-drift gates and Phase D pre-flight. Purely additive — no
schema migration (generic property graph).

**v2 (data-science aligned):** beyond code structure, the graph now models data as
first-class citizens — `dataframe`/`column`/`dataset` nodes with **dtypes + provenance**,
`api_source` nodes, ML overlay (`split`/`model`/`hyperparameter`), DE `lineage`
(upstream→op→downstream), and sub-flow `profile` tags (function-calling, pipeline,
data-flow, ml-training, data-engineering, endpoint). `data-flow` requires **real
flow** (a consumed produced value, or a consumed input) — merely returning a value
nobody uses does NOT qualify; route handlers are tagged `endpoint` instead. The
visualization shows the full graph by
default with a **sub-flow lens** and **click-a-node end-to-end focus**. New selfcheck
gates enforce dtype presence (warn), end-to-end dtype consistency (error), and lineage
integrity (error).

**Boundary:** This skill covers *building and registering* a project state graph and stops at *querying it for a specific task* (that's normal analysis work against the produced DB).

## When to Use

- Starting a new project / app / initiative and want a baseline map.
- Onboarding an unfamiliar existing repo before making changes.
- Refreshing the graph after significant code changes.

**When NOT to use:** day-to-day querying of an already-built graph — just `SELECT` from the deep DB. Building skills themselves → `writing-skills`.

## The 5-Part Flow (do all five, in order)

1. **Record the project** — decide a unique `--name` and the `--repo` path.
2. **Build the deep layer** — the analyzer produces `<name>-state-graph.db`.
3. **Build the shallow layer** — `ARCHITECTURE.md` is generated from the DB.
4. **Verify** — `selfcheck` invariants must PASS (non-empty node types, no dangling edges, cards match callables, commit_sha set).
5. **Finalize** — registry (`projects.json`) and global index (`PROJECT-STATE-GRAPHS.md`) updated.

`init_project.sh` performs all five deterministically:

```bash
bash ~/.code_puppy/skills/project-state-graph/scripts/init_project.sh \
  --name <project-name> --repo <repo-path> [--out-dir <dir>]
```

Default `--out-dir` is `~/skill-workspace/project-graphs/<name>/`.
On success you get the deep DB + `ARCHITECTURE.md` in the out dir, a registry entry,
the regenerated index, and a `Self-check: PASS` line. Non-zero exit on any failure.

## Agent profiling phase (data-science routing + gap-only dtype probes)

`init_project.sh` is fully deterministic and **always runs every analyzer**
(code, data-flow, data-model, API, pipeline, profiles, and the ML/DE overlays).
On top of that, the agent performs a lightweight **profiling phase** before/around
the build to maximise data coverage:

1. **Inspect repo settings** — read `pyproject.toml`/`requirements`, imports, and
   a sample of modules to decide which sub-flows are present (pandas, pyspark,
   sklearn/torch, SQL/BigQuery, HTTP APIs).
2. **Static first.** The analyzers capture dtypes from annotations, `.astype()`,
   `dtypes=`, `StructType`, pandera/pydantic schemas, and literal `df["col"]`
   access. Every typed node records a `dtype_provenance` on the ladder
   `declared-schema -> annotation -> static-inference -> runtime-probe -> unknown`.
3. **Probe only the gaps.** For `column`/`data_var` nodes left `dtype = "unknown"`,
   and ONLY those, the agent may emit a tiny throwaway probe script **in the
   project's own venv** that imports the code, builds/inspects a small sample
   frame, prints the dtype, then records it back onto the node with
   `dtype_provenance = "runtime-probe"`. Never run more than needed; never probe
   what static analysis already resolved.
4. **Audit.** Because every node says HOW its type is known, a reviewer can trust
   `declared-schema`/`annotation` immediately and scrutinise `runtime-probe` /
   `unknown`.

This keeps determinism (the build never depends on probes) while letting the
agent enrich coverage where it matters.

## Self-check severity model

`selfcheck.py` runs deterministic invariants in two tiers; the build exits
non-zero (and `init_project.sh`, under `set -e`, aborts) only on an **error**:

| Invariant | Severity | Meaning |
|---|---|---|
| `node_types_nonempty` | error | graph isn't empty |
| `no_dangling_edges` | error | every edge endpoint references a real node |
| `cards_match_callables` | error | one consistency+symbol card per function/method |
| `commit_sha_set` | error | the run recorded a git SHA |
| `no_undefined_symbols` | **error (HARD)** | no `unresolved_call` nodes — a bare-name call (`foo()`, never `obj.foo()`) that resolves to nothing (not a project callable, import, builtin, or local). Conservative: a rename/typo trips it; legit imports/builtins/methods never do. **Blocks the build.** |
| `no_isolated_nodes` | **warning (yellow)** | function/method nodes with no behavioral edge (dead code). Printed as `[WARN]`, **never blocks**. |
| `dtype_present` | **warning (yellow)** | a `column`/`data_var` left `dtype=unknown` and not `runtime-probe`d. Surfaced, **never blocks** (keeps always-green on messy repos). |
| `dtype_consistency_e2e` | **error (HARD)** | walks `produces`/`consumes`; a produced dtype that disagrees with a consumer's expected dtype is an end-to-end data break. **Blocks the build.** |
| `lineage_no_dangling` | **error** | every `derives`/`transforms`/`feeds`/`lineage` edge endpoint resolves to a real node. |
| `profile_assigned` | **warning (yellow)** | application callables with no `tagged_profile` sub-flow tag. Non-blocking. |

`run()` sets `ok = all(c.ok for c in checks if c.severity == 'error')`, so warnings
are surfaced but never change the exit code.

## DataFrame-aware slices (Phase B)

Beyond the whole-graph `visualize.py`, a peer renderer `viz_slices.py`
(`analyzer/slice_viz.py`) emits **four per-perspective slices** into ONE
self-contained, `file://`-safe HTML file so a human can *eyeball-validate* the
graph (a hairball of hundreds of nodes is unreviewable). Stdlib-only; no analyzer
imports (byte-portable into the reviewer skill, like `graph_viz.py`).

| # | Slice | What it shows |
|---|---|---|
| 1 | **Dataflow + datatype** | data_var/column nodes along produces/consumes/feeds, each labeled with dtype. **Unknown dtypes render gray (`#9aa3af`) with a `?`** so coverage holes are visible at a glance. A dtype-coverage badge (`N% typed`) sits in the header. |
| 2 | **Function call chain** | a focus function's callers (above) + callees (below) within N hops — the neighborhood, not the whole graph. Interactive focus box. |
| 3 | **Pipeline view** | `pipeline_step` edges grouped by pipeline, in execution order (hierarchical LR). |
| 4 | **API surface** | a **table** (not a diagram): path · method · handler · what the handler calls. |

`init_project.sh` runs this automatically as stage **[5/5]** (additive,
non-fatal) -> `<out-dir>/<name>-slices.html`. The unknown=gray convention also
feeds the Phase C-2 dtype-coverage metric (gate strength == dtype coverage).

## Cold backup + dtype coverage (Phase C)

Two low-cost protections run automatically during `init_project.sh`:

- **C-1 · Cold snapshot.** Before the deep-layer rebuild wipes the old DB,
  `archive_db.sh` copies it to `provledger.<old_sha>.db` (sha from the prior
  `analysis_run`, timestamp fallback). It is a **cold archive — not in any query
  path** — the raw material for future version-over-version provenance ("how did
  this symbol change across versions?") and the ledger's fuzzy search. The delta
  logic can come later; the snapshots **cannot be captured retroactively**.
  Non-fatal: a failed archive never blocks the rebuild.
- **C-2 · dtype coverage as a tracked metric.** Every data gate is exactly as
  strong as dtype coverage — a `data_var` left `dtype=unknown` makes its check
  pass through silently (green where it is actually blind). `selfcheck.py` now
  prints a headline **`dtype coverage: N% (typed/total typed)`** line (warning
  severity, never blocks) alongside the per-node `dtype_present` detail. The same
  number shows on the Phase B dataflow slice badge.

## Quick Reference

| Action | Command |
|---|---|
| Init / refresh a project graph | `init_project.sh --name N --repo R` |
| Build deep layer only | `uv run python -m analyzer <repo> --project N --db-path P` |
| Generate ARCHITECTURE.md only | `uv run python architecture_md.py <db> <name> <out>` |
| Render the 4 DataFrame-aware slices | `uv run python viz_slices.py <db> <out.html> [--title N] [--focus QNAME]` |
| Cold-snapshot a DB before overwrite | `bash archive_db.sh <db>` |
| Verify a built DB (+ dtype coverage %) | `uv run python selfcheck.py <db>` |
| List projects on record | read `~/skill-workspace/project-graphs/projects.json` |

All Pythony points run inside the skill's `uv` env (`cd scripts && uv run ...`).

## Common Mistakes

- **Skipping verification.** Always confirm `Self-check: PASS` — a graph that fails invariants is not trustworthy.
- **Committing generated graphs.** `*.db` and `*-state-graph.*` are git-ignored in the skill dir; keep graphs under `~/skill-workspace/project-graphs/`, never in the skill or the target repo.
- **Re-running on a dirty tree.** The analyzer warns when the repo has uncommitted changes — the graph may not match committed code.
