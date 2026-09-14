# Writing a node-type provider — and proving it behaves

provLedger's code graph is built from **node-type providers**. The built-in
ones (`provledger.symbol` for functions / methods / classes,
`provledger.owned` for dataframes / columns) are ordinary providers written
against the same public API a third party gets. A provider *extracts*: it
looks at a repository and returns observations. The **host owns identity,
history, tier and propagation** — matching across runs, node keys, events and
tiers are never a provider's business.

## The contract (`provledger.graph_api`)

```python
from provledger.graph_api import MUTATIONS, NodeObservation, Signature

class DatasetComments:
    type_id = "acme.dataset_comments"      # vendor.name — namespaced, lower case
    schema_version = 1
    requires = ()                          # capabilities: "runtime_capture" | "network" | "llm"
                                           # (the host offers none yet: declaring one degrades you)

    def extract(self, ctx) -> list[NodeObservation]:
        out = []
        for rel in sorted(ctx.file_map):                  # ctx.repo_root, ctx.conn_ro, ctx.run_id, ctx.node_rows(kinds)
            if not rel.endswith(".py"):
                continue
            mod = rel[:-3].replace("/", ".")
            for n, line in enumerate(open(f"{ctx.repo_root}/{rel}", encoding="utf-8"), 1):
                if line.startswith("# @dataset:"):
                    name = line.split(":", 1)[1].strip()
                    qn = f"{mod}:{name}"
                    out.append(NodeObservation(
                        type_id=self.type_id, node_type="dataset", qualified_name=qn,
                        file_path=rel, line_start=n, line_end=n,
                        signatures=(Signature("qualname", qn),),     # layers: qualname | struct | dataflow | x-<custom>
                        attrs={"source": "comment"}))
        return out

    def attributes_schema(self):
        return {"required": ["source"], "types": {"source": "str"}}   # "tier" is always forbidden

    def declared_stability(self):
        return {m: "preserved" for m in MUTATIONS}                     # what each mutation does to YOUR nodes
```

- `ctx.conn_ro` is a **read-only** connection to the graph being built (this
  run's rows via `ctx.node_rows(("function", "method"))`); `ctx.file_map` maps
  repository-relative paths to file node ids. Never write, never touch files.
- `signatures`: the host matches on the `qualname` layer first, then `struct`
  and `dataflow` (1:1 groups only; larger groups are recorded as ambiguity,
  never guessed). A layer with `value=None` is unavailable; `trivial=True`
  never matches. Custom `x-…` layers are accepted but not matched (yet).
- `attrs` must satisfy your own `attributes_schema()`; the host adds
  `"tier"` to the forbidden list — every observation from `extract()` is
  observed-tier by construction.
- `owner_qn` / `name` mark an **owned** node (identity = owner + local name):
  the host re-links it when its owner is paired.

## Running it

The host calls every provider through `providers.run_provider`: an exception
→ the provider is *degraded* for the run (empty result, reason recorded),
a timeout (default 30 s, `timeout_s` in the registration) → degraded, a
result that violates your schema / claims another `type_id` / uses an
unknown layer → the **whole** result set is rejected. Partial results never
reach the graph.

Register it in `provledger-extensions.json` (see `docs/extensions.md` §8):

```json
{"version": 1,
 "providers": [{"id": "acme.dataset_comments", "module": "acme_provider:DatasetComments", "priority": 5}]}
```

## Proving it: `provledger.testing.conformance`

```python
from provledger.testing import conformance
report = conformance.run(DatasetComments())        # the shipped mutation corpus, package-native context
print(report.text()); assert report.ok
```

Six contracts, each a line of the report:

| check | what it demands | what a failure looks like |
|---|---|---|
| `determinism` | extracting the same repository twice gives byte-identical observations | `basic_pipeline: two extractions of base differ` |
| `purity` | no write SQL on `conn_ro`, no file under the repository created/changed | `extract issued write SQL: [...]` / `extract changed files under the repo: ['touched.txt']` |
| `stability_matches_declaration` | for every corpus mutation that reaches your nodes, what the host matcher observes (preserved / broken / ambiguous) equals `declared_stability()` — on every case | `basic_pipeline/rename_function: declared preserved, observed broken` |
| `schema` | namespaced `type_id`, attrs valid, known or `x-` layers | `attribute 'tier' is forbidden` |
| `failure_isolation` | an injected exception (after a partial result) degrades you, nothing propagates or leaks | `injected failure was not isolated` |
| `performance_budget` | the slowest base extraction stays inside `timeout_s` (warning) | `slowest base extraction 41.20s vs budget 30.0s — over budget` |

**Honesty, not stability.** A provider whose identity breaks on
`rename_variable` passes as long as it *says* so. The built-in owned
provider declares `rename_variable: broken` — renaming the variable that
holds a dataframe is a new data node by design. A corpus case a mutation
never reaches is no evidence either way. What fails is the lie: the package
ships `testing.liar_provider` (its struct signature mixes the function name
in, yet it declares `rename_function: preserved`) and the suite names the
mutation that lied.

Providers whose nodes only make sense next to a graph (like the built-ins)
pass a `context_factory` that builds one, and `companions` — providers whose
observations join the match so owned nodes can inherit identity:

```python
conformance.run(BuiltinOwnedProvider(), context_factory=build_graph, companions=[BuiltinSymbolProvider()])
```

## What the run records

`analysis_run.extensions_json.providers` lists every provider of the run —
declared or built-in, loaded or degraded — with `{id, module, schema_version,
enabled, priority, degraded, observations, elapsed_s}`; `selfcheck` warns
`providers_degraded` when any of them degraded. Snapshot rows carry
`type_id` and `schema_version` in their attrs, so a node can always be traced
back to the provider (and its schema) that observed it.
