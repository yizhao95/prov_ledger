# Extending provLedger without touching its source

provLedger reads **one** JSON file, `provledger-extensions.json`, to register
three kinds of things that used to require editing the source:

| what | where it is used | example |
|---|---|---|
| **constraints** anchored to data points | surfaced at plan time, checked at close (a change to an anchored node that never read the constraint is recorded as `constraint_bypassed`) | "clean() must keep dropping zero-quantity rows" |
| **name sets** the code analyzer recognises | which calls count as a train/test split, a model fit, an evaluation, a validator, a DataFrame constructor, an HTTP verb | your `my_split()` becomes a split node |
| **drift kinds** over profile snapshots | `detect_drift` reports them next to the built-in kinds | "null fraction rose by 0.1 or more" |
| **node-type providers** (phase 6) | the graph builder runs your `NodeTypeProvider` next to the built-in ones; `docs/conformance.md` proves it behaves | `# @dataset:` comments become `dataset` nodes |

Everything is **explicit**: nothing is auto-discovered from installed packages,
and only one file applies at a time. Every analysis records which file (and
which sha256) was in force, so a graph or a drift report can always be traced
back to the registrations behind it.

## 1 · Where the file lives (discovery)

The first of these that exists is used — the others are ignored, never merged:

1. `<repo>/provledger-extensions.json` — the repository being analysed
2. the file named by `$PROVLEDGER_EXTENSIONS`
3. `~/skill-workspace/provledger-extensions.json`

A malformed file is an error (nothing is applied), never a silent fallback.
The repository root ships an example: `provledger-extensions.example.json`.

## 2 · The file

```json
{
  "version": 1,
  "drift_kinds": [
    {"id": "acme.null_spike_strict", "metric": "null_frac", "op": "delta_gte", "value": 0.1, "priority": 10},
    {"id": "acme.rows_halved",       "metric": "row_count", "op": "dropped_below", "value": 0.5}
  ],
  "namesets": [
    {"set": "split_funcs", "add": ["my_split", "time_split"]},
    {"set": "fit_methods", "add": ["fit_transform_all"], "remove": ["train"]}
  ],
  "constraints": [
    {"project": "prov_ledger",
     "statement": "orders.region != 'X' must stay excluded",
     "subjects": ["pkg.pipeline.clean", "orders.region"],
     "keywords": ["scope-change", "finance"],
     "why_ref": "https://wiki/decisions/42",
     "why_visibility": "restricted"}
  ]
}
```

`version` is `1` (omit it or write `1`). Every section is optional.

### Ids and priorities

- Drift kind ids are namespaced **`vendor.name`** (`^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$`).
  A duplicate id fails the load — ids are never silently overridden.
- `priority` is an integer, default `0`, **larger applies first**. Two drift
  kinds with the same priority on the same target (same `metric` and `op`)
  fail the load; so do two `namesets` entries with the same priority on the
  same set. Name-set entries of one set apply in ascending priority, so the
  highest-priority entry lands last and wins (`remove` at 5 beats `add` at 1).

## 3 · Drift kinds

A drift kind is a deterministic predicate over one column's profile before
and after a change. Profile rows look like
`{"dataset": "orders", "column_name": "qty", "dtype": "int64", "null_frac": 0.0, "row_count": 1200, "distinct_count": 40}`.

| field | values |
|---|---|
| `metric` | `dtype`, `null_frac`, `distinct_count`, `row_count`, `mean`, `min`, `max` (a metric missing from a row never fires) |
| `op` | `delta_gte` / `delta_lte` (after − before compared with `value`), `eq` (after == value), `changed` (before != after; no value needed), `became` (after == value and before != value), `dropped_below` / `rose_above` (after ÷ before compared with `value`; skipped when before is 0 or missing) |
| `enabled` | `true` (default) / `false` |

Declared kinds run **after** the built-in ones (`dtype_changed`, `null_spike`,
`cardinality_collapse`, `column_added`, `column_dropped`), in priority order,
at most one row per column per kind. Every drift row carries `source`:
`"builtin"` or `"extension:<id>"`. A declared kind is observed-tier like the
built-ins (its predicate is deterministic); extensions cannot declare
asserted/derived facts.

Run it from Python (the backend is stdlib-only: set
`PYTHONPATH=<provledger checkout>/orchestrator-backend`, or install the
`provledger` wheel). `before_rows` / `after_rows` are plain lists of
profile-row dicts — e.g. `json.load(open("profiles/before.json"))`:

```python
from orchestrator import drift, extensions

ext = extensions.current("/path/to/repo")            # load(discover(repo)); EMPTY when no file
report = drift.detect_drift(before_rows, after_rows, extensions=ext)
for d in report:
    print(d["kind"], d["column"], d["before"], "->", d["after"], d["source"])
```

`detect_drift` itself never discovers the file — pass `extensions=` (or use
`run_data_decision_loop`, which defaults to the discovered one). The close-time
observation of `profile_drift` expectations applies the project's file too.

Built-in thresholds, so you can tell when a declared kind duplicates one:
`null_spike` fires when `null_frac` rises by **0.3** or more (`after - before
>= 0.3`); `cardinality_collapse` when `distinct_count` becomes 1 with more
than one row; `dtype_changed` on any dtype change; `column_added` /
`column_dropped` on presence. A declared `null_frac` / `delta_gte` / `0.1`
kind therefore fires on 0.0 → 0.15 while `null_spike` does not.

## 4 · Name sets

The analyzer recognises code patterns by name. The sets and their defaults:

| set | default |
|---|---|
| `split_funcs` | `train_test_split` |
| `fit_methods` | `fit`, `train` |
| `eval_methods` | `predict`, `score`, `evaluate` |
| `hp_names` | `param_grid`, `params`, `config`, `hyperparams`, `hparams`, `parameters`, `search_space` |
| `validator_names` | `validate`, `check`, `check_schema`, `expect`, `assert_schema` |
| `ml_call_names` | `train_test_split`, `fit`, `train` |
| `df_constructors` | `DataFrame`, `createDataFrame` |
| `http_read_methods` | `get`, `head`, `options` |
| `http_write_methods` | `post`, `put`, `patch`, `delete` |
| `http_libs` | `requests`, `httpx`, `aiohttp`, `session`, `client` |

`add` / `remove` patch a set; an unknown set name fails the load (the message
lists the known sets). The file is read from the **repository being analysed**
when the graph is built:

```bash
cd <provledger checkout>/skills/project-state-graph/scripts
bash init_project.sh --name myproj --repo /path/to/repo       # builds the graph + registers the project
# or, without the registry:
uv run python -m analyzer /path/to/repo --project myproj --db-path /tmp/myproj.db
```

An untracked `provledger-extensions.json` makes the analyzer print
`WARNING: working tree ... has uncommitted changes`; that is about git
cleanliness, not about the file — it was read. Commit the file to silence it.

With `{"set": "split_funcs", "add": ["my_split"]}` in the repo, a line such as
`X_train, X_test = my_split(df)` inside a function `run()` produces two
`split` nodes named `<enclosing function>:<role>` — `run:train` and
`run:test`, with `metadata_json = {"role": "train"}` / `{"role": "test"}` —
exactly as `train_test_split` would. Check with sqlite (no `sqlite3` CLI
needed):

```bash
python3 -c "import sqlite3;print(*sqlite3.connect('/tmp/myproj.db').execute(\"SELECT n.name,n.metadata_json FROM node n JOIN node_type t ON t.id=n.node_type_id WHERE t.name='split'\"),sep='\n')"
# ('run:train', '{"role": "train"}')
# ('run:test', '{"role": "test"}')
```

## 5 · Constraints

A constraint is anchored to data points by `subjects`: a qualified name
(`pkg.pipeline.clean`), an `owner.column` (`orders.region`) or a node key
(`nk_…`). Anchors match **exactly**; `keywords` are the lexical side (they, and
bare-name subjects, drive the plan-time reminders). `why_visibility:
"restricted"` keeps the rationale inside the ledger — only `why_ref` ever
surfaces. There is no taxonomy: `keywords` are free text.

Constraints are imported into the project's ledger — declaratively, and
idempotently (a second import of the same file skips what is already there):

```bash
bash skills/writing-plans/scripts/ledger-add.sh import provledger-extensions.json --project myproj
# {"imported": 2, "skipped": 0, "unresolved_subjects": ["orders.region"], "project": "myproj", ...}
```

A constraint's `project` may be omitted (then `--project` is used); when
given it must equal `--project`. Subjects that resolve in the registered
graph get their node key appended (the text is kept); `unresolved_subjects`
lists the rest — usually a name that does not exist (yet) in the graph.

At plan time a constraint anchored to a declared target is surfaced in the
plan's impact context (a restricted rationale is not). At close, a changed
anchored node whose constraint was never surfaced is recorded as
`constraint_bypassed` on the node and `[CONSTRAINT BYPASSED]` in the review
log — visible, never blocking.

## 6 · Reproducibility

Every analysis run records the extension set in force in
`analysis_run.extensions_json`:

```json
{"path": ".../provledger-extensions.json", "sha256": "…", "drift_kinds": ["acme.null_spike_strict", "acme.rows_halved"],
 "namesets": {"split_funcs": ["my_split", "time_split"], "fit_methods": ["fit_transform_all", "-train"]}, "constraints": 1}
```

`null` when no file applied. Read it with
`SELECT id, extensions_json FROM analysis_run`. Two runs over the same file
record the same sha256; the history CLI shows it per event — the node argument
is the dotted qualified name (file path relative to the repo with `/` → `.`
and `.py` dropped, plus the function: `src/train.py::my_split` →
`src.train.my_split`), `ext=` is the first 8 hex characters of the sha256:

```bash
cd <provledger checkout>/skills/project-state-graph/scripts && uv run python -m analyzer history /tmp/myproj.db src.train.my_split
#   run=1 seq=2 node_added [observed] plan=- step=- trigger=manual sha=3627ed9 ext=c62894c8 at=… payload=…
```

## 7 · Providers (node types)

A third-party **node-type provider** registers here too — the graph builder
runs it next to the built-in ones. How to write one, and how to prove it
keeps the six contracts, is in [`docs/conformance.md`](conformance.md).

```json
{
  "version": 1,
  "providers": [
    {"id": "acme.dataset_comments", "module": "acme_provider:DatasetComments", "enabled": true, "priority": 5, "timeout_s": 30},
    {"id": "provledger.owned", "enabled": false}
  ]
}
```

| field | meaning |
|---|---|
| `id` | `vendor.name`; must equal the class's `type_id`; a duplicate id fails the load |
| `module` | import path `pkg.mod:Class`. The interpreter that runs the analyzer must be able to import it — put the module's directory on `PYTHONPATH` (`PYTHONPATH=/path/to/module_dir uv run python -m analyzer …`); the analyzer's own `uv run` environment already carries `provledger`, so `from provledger.graph_api import …` inside your module works there. A built-in id (`provledger.symbol`, `provledger.owned`) may omit it |
| `enabled` | default `true`; `false` disables — the only way to switch a built-in off |
| `priority` | integer, larger runs first (built-ins are 0) |
| `timeout_s` | budget per extraction (default 30); over budget → degraded |

Nothing here raises at analysis time: an import that fails, a class that is
not a `NodeTypeProvider`, a `type_id` that differs from the declared id, a
required capability the host does not offer (`requires`), an exception, a
timeout or a schema violation each become a **degradation record** — the run
continues without that provider, the analyzer prints
`WARNING: provider <id> degraded: <reason>` on stderr,
`analysis_run.extensions_json.providers` says what happened
(`SELECT extensions_json FROM analysis_run`), and `selfcheck` warns
`providers_degraded`:

```bash
cd <provledger checkout>/skills/project-state-graph/scripts && uv run python selfcheck.py /tmp/myproj.db
```

## 9 · Error messages

| message | cause |
|---|---|
| `drift_kinds[0]: id must be namespaced vendor.name` | id without a dot / uppercase / leading digit |
| `duplicate id 'acme.x' in drift_kinds` | the same id twice |
| `drift_kinds 'acme.a' and 'acme.b' share priority 3 on the same target (null_frac/eq)` | same priority, same metric and op |
| `metric 'colour' is not one of [...]` / `op 'bogus' is not one of [...]` | unknown metric / op |
| `op 'eq' needs a value` | every op except `changed` needs `value` |
| `namesets: unknown set 'splits'; known sets: [...]` | typo in the set name |
| `constraints[0]: why_visibility 'secret' is not one of ['shared', 'restricted']` | visibility |
| `constraints[0] declares project 'other' but --project is 'myproj'` | import into the wrong project |
| `version 2 is not supported` | this reader understands version 1 |
| `not valid JSON` | trailing commas / comments — JSON, not JSON5 |
| `providers[0] acme.x: module must be an import path like pkg.mod:Class` | registration without `pkg.mod:Class` |
| `providers[0]: module is required (...)` | a non-built-in id without a module |
| `duplicate id 'acme.x' in providers` | the same provider id twice |
| (run record) `import failed: ModuleNotFoundError: ...` / `capability 'llm' unavailable on this host` | a provider that could not be loaded — recorded, never raised |

Errors are raised by the loader before anything is applied.
