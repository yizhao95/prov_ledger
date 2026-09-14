# Arbitration — linking identities the matcher will not

The state-graph matcher (`provledger.graph_api.match`) links a node across
runs by four deterministic layers (qualified name, structural signature,
dataflow signature, owner). When two or more nodes on both sides are
indistinguishable on the layer that would decide — two body-identical
helpers that were both renamed — it refuses to guess and records
`identity_ambiguous` (tier `observed`); the new nodes get provisional keys and
are **never silently linked**.

An **arbiter** may resolve such an ambiguity — a heuristic, a person, or
one day a model — and its answer is written as `identity_asserted` (tier
`asserted`, with `evidence` and the `arbiter` id). This document is the bar
an arbiter must clear before the analyzer lets it write anything.

## 1 · The interface

```python
from provledger.graph_api import Arbiter, Ambiguity, Assertion

class MyArbiter:                      # satisfies graph_api.Arbiter (a runtime-checkable Protocol)
    arbiter_id = "acme.my_arbiter"    # vendor.name; the report is filed under it

    def arbitrate(self, ambiguities: list[Ambiguity]) -> list[Assertion]:
        ...                           # Assertion(cur_qualified_name, chosen_prev_key, evidence, arbiter)
```

`Ambiguity` carries `layer` and the `prev` / `cur` rows (qualified name,
file, lines, struct / dataflow signatures, node keys). An `Assertion` with
empty `evidence`, or one that names a key outside the ambiguity, is refused
by `history.resolve` (ValueError — nothing is written).

The reference implementation is `provledger.testing.heuristic_arbiter.HeuristicArbiter`
(`arbiter_id = "provledger.heuristic"`): it links a current node to the
previous node whose *name* is nearest by edit distance, only when that
minimum is unique and the struct and dataflow signatures are equal, with
evidence `"name distance d, struct/dataflow sigs equal"`. Otherwise it
abstains.

## 2 · The calibration file

Export every `identity_ambiguous` event of a graph as calibration items:

```bash
cd skills/project-state-graph/scripts
uv run python -m analyzer ambiguities ~/skill-workspace/project-graphs/<p>/<p>-state-graph.db \
    --export ~/skill-workspace/arbiter-eval/calibration.json --repo <repo>
```

```json
{"version": 1, "source": {"db_path": "...", "repo": "...", "events": 19},
 "items": [
  {"id": "e123",
   "ambiguity": {"layer": "struct_sig", "run_id": 45, "prev_run_id": 44, "commit_sha": "...", "prev_commit_sha": "...",
                 "same_struct_sig": true, "same_dataflow_sig": true,
                 "prev": [{"qualified_name": "pkg.m.norm_a", "node_key": "nk_…", "node_type": "function",
                           "file_path": "pkg/m.py", "line_start": 3, "line_end": 5,
                           "struct_sig": "…", "dataflow_sig": "…", "context": "    1  …±10 lines…"}, …],
                 "cur":  [{"qualified_name": "pkg.m.scale_a", "node_key": "", …}, …]},
   "truth": null,
   "labelled_by": null}
 ]}
```

A person fills `truth` for the items they can judge:

- `{"pairs": [["prev_qn", "cur_qn"], …]}` — these previous nodes became these current ones (an exact set);
- `"none"` — none of them are the same node (all genuinely new / removed);
- `null` — cannot tell; the item counts for consistency and coverage, not accuracy.

`context` is read from the run's commit (`git show <sha>:<path>`), so the
old side shows the old source even after the file changed.

## 3 · The evaluation

```bash
uv run python -m analyzer arbiter-eval calibration.json --arbiter provledger.testing.heuristic_arbiter:HeuristicArbiter
# arbiter=provledger.heuristic items=19 labelled=12 runs=3 consistency=1.000 coverage=0.632 accuracy=0.917 evidence_ok=True sha=3f0a…
# report: ~/skill-workspace/arbiter-eval/provledger.heuristic.json
# gate: passed (consistency 1.00, accuracy 0.92 on 12, sha 3f0a…)
```

`provledger.testing.calibration.run(arbiter, calib_path, n_runs=3)` replays
the arbiter over every item `n_runs` times and writes
`<eval dir>/<arbiter_id>.json` (`PROVLEDGER_ARBITER_EVAL_DIR`, default
`~/skill-workspace/arbiter-eval`):

| number | meaning | bar |
|---|---|---|
| `consistency` | share of items answered identically on every run | **must be 1.0** — a model that wavers is out |
| `coverage` | share of items with at least one assertion | reported, no bar (abstaining is legitimate) |
| `accuracy` | on labelled items: the answer set equals `truth` exactly (`"none"` ⇔ no assertion) | **≥ 0.9 on ≥ 10 labelled items** |
| `evidence_ok` | every assertion carried non-empty evidence | must be true (empty-evidence assertions are dropped and flagged) |
| `sha` | sha256 of the calibration file evaluated | must equal the file in use when the analyzer runs |

## 4 · The gate

The analyzer never imports an arbiter on its own. Name one:

```bash
export PROVLEDGER_ARBITER=provledger.testing.heuristic_arbiter:HeuristicArbiter
export PROVLEDGER_ARBITER_CALIB=~/skill-workspace/arbiter-eval/calibration.json   # default: <eval dir>/calibration.json
uv run python -m analyzer <repo> --project <p> --db-path <db>
```

`cli.run` loads the class, reads its report and applies the bar above. The
decision is recorded on the run — `analysis_run.extensions_json.arbiter`:

```json
{"id": "provledger.heuristic", "spec": "…:HeuristicArbiter", "calibration": "…/calibration.json",
 "gate": "refused: accuracy 0.8 < 0.9 on 10 labelled items"}
```

- **refused** (no report · consistency < 1.0 · an assertion without evidence
  · fewer than 10 labelled items · accuracy < 0.9 · report sha ≠ calibration
  file · the class cannot be loaded): the arbiter is **not wired**, the
  resolver runs exactly as without one, the analyzer prints
  `WARNING: arbiter <id> not wired: refused: …` and `selfcheck` warns
  `arbiter_gate`.
- **passed**: `history.resolve` receives `arbitrate`; every accepted
  assertion becomes an `identity_asserted` event (tier `asserted`) *next to*
  the `identity_ambiguous` it resolves — the observation stays on the record.

Refusal is a legitimate result, not an error: it means the calibration set
or the arbiter is not there yet. `test_llm_consistency.py` (deselected by
default) runs the same `calibration.run` against whatever
`PROVLEDGER_ARBITER` names.

## 5 · What is deliberately not here

No model is wired in this repository. The heuristic is the baseline and the
worked example of the interface; a model-backed arbiter is a later phase
(FUTURE-LOG) and has to clear the same bar on the same file.
