# Decision provenance — why a node changed, and where the why came from

provLedger already records **what** changed (the state graph's history) and
**that** a plan changed it. Decision provenance is the layer underneath:
for every changed node, *why* — and, for every why, *its source level*,
decided by structure, not by whoever wrote it. This document is the 0/1
phase: the tables, the source levels, the three ways a reason enters, the
rules that fill reasons on their own, and the honest boundaries.

## 1 · What it is for (D1)

A reason is worth recording only if a person can later tell **the user's
words** from **the agent's reading** from **a rule's inference** from **an
admitted gap**. Everything here serves that one distinction; the wording is
neutral on purpose — source, context, traceable — never blame.

## 2 · The tables (migration 018)

| table | holds | written by |
|---|---|---|
| `utterance` | the user's words, verbatim, with `occurred_at` and the DB's `recorded_at` | the `UserPromptSubmit` hook, `provledger note` |
| `reference` | a pointer to a source — kind (email / meeting / chat / ticket / doc / commit / verbal / other), label ≤ 512, optional uri, `verifiability` linked / verbal / unreachable | `provledger note --ref`, the ledger import |
| `change_reason` | why a node changed: `role` reason / rejected_path / constraint, `tier`, an optional verbatim span, `interpretation` / `statement` / `rationale`, `occurred_at` vs `recorded_at`, `recorded_by` human / agent / system, `rule_id` | `reason-fill.sh`, the rules, the backstop, `note --node`, the migration |
| `reference_link` | reason ↔ reference | with the reason |
| `trigger_log` | every rule verdict: auto / ask / silent, with its basis | `triggers.evaluate` |
| `tool_call_log` | one row per tool call (phase 0) | the `PostToolUse` hook |

All of them are append-only: no DELETE, and the only UPDATEs the triggers
allow are `change_reason.superseded_by` (a correction is a new row that the
old one points at) and `reference.last_checked`. Each table carries a hash
chain (`provenance.verify_chain`).

## 3 · The four tiers and the four source levels

**tier** is *how the reason came to be*, enforced by CHECK constraints:

| tier | means | requires |
|---|---|---|
| `stated` | the user said it | a `verbatim_utterance_id` + span that exists in that utterance — nothing else can be stated |
| `asserted` | someone's reading of it (agent or person) | an `interpretation` or `statement` |
| `derived` | a rule inferred it from the ledger and the graph | a `rule_id` |
| `unstated` | nobody knows — recorded as a gap | no text at all |

**来源等级 / source level** (`evidence_level` in `change_reason_v`) is *what
the reason can be checked against*, computed from the row's links, never
stored:

| level | when |
|---|---|
| `linked` | a linked reference (has a uri) |
| `verbal` | a verbatim span, or a verbal reference |
| `task_context` | only the plan / step / run / time it was recorded in |
| `unstated` | the tier is unstated |

Every recorded reason is at least `task_context`, because it hangs on the
plan that closed.

## 4 · How a reason enters

1. **The hook** (`hooks/utterance.sh`): every prompt you send is one
   `utterance` row, verbatim, attributed to the repo you are in and its open
   plan. Empty prompts and slash commands are skipped. The hook never writes
   stdout and never fails loudly; failures are counted by `selfcheck
   hook_failures`.
2. **Close-time fill** (`reason-slots.sh` / `reason-fill.sh`): for each node
   the plan changed, one answer of exactly one shape —
   `{node_key, utterance_id, span}` (stated), `{node_key, interpretation,
   refs?}` (asserted), `{node_key, unstated: true}`. The old `{node_key,
   text}` shape exits 2. `reason-slots.sh` with `"draft": true` proposes the
   utterances that mention the node. The prompt to answer is *why not the
   other way, and who asked for it* — not what the step did.
3. **The rules** (`orchestrator/triggers.py`): before anyone is asked, R1–R6
   run over every node the plan's runs touched; a hit writes a `derived`
   reason with the rule id and its basis, and the node is not asked about.

   | id | rule |
   |---|---|
   | R1 | a test failed then passed in this plan and names the node's file |
   | R2 | the previous review of this project failed a gate on the node |
   | R3 | a drift decision on a dataset the node reads fell in the plan window |
   | R4 | pure refactor: identity kept, renamed or moved, struct_sig unchanged |
   | R5 | an active constraint is anchored on the node |
   | R6 | a failed step recovered by a deviation → a `rejected_path` (command + error tail) |

   Every verdict is one `trigger_log` row: `auto` (a rule filled it), `ask`
   (nothing recognised the change; it becomes a slot), `silent` (the project
   closes in `pending` mode). `provledger-extensions.json` →
   `reasons.close_mode` chooses `ask` (default) or `pending`.
4. **After the fact** (`provledger note`):

   ```bash
   provledger note "finance wants fiscal weeks, not calendar weeks" --at "2026-09-10 14:30" \
       --project prov_ledger --node pkg.rollup.weekly --ref kind=verbal,label="hallway with the CFO"
   ```
   The words become an utterance whose `occurred_at` is the time you give;
   `recorded_at` is the database's. `--node` records a stated reason spanning
   the whole note; `--ref` registers sources (a verbal one has no uri and
   is `verbal`; an email without a uri is `unreachable` until someone finds
   it; a uri makes it `linked`).

## 5 · What happened to the old rows

Before this phase `node_reason` called every agent sentence `stated`. The
migration (`provenance_migrate`, run once when the database is opened)
copies each row into `change_reason` as `asserted` (the text becomes the
interpretation) — or `unstated` when the text was NULL, or `derived` with
`rule_id = legacy` — and writes one `trigger_log` row per copy saying where
it came from. Constraints from `LedgerEntries` become `role = constraint`
rows (one per anchored subject) with their `why_ref` registered as an
unreachable doc reference; a restricted rationale stays `personal`. The old
tables are read, never rewritten; `node_reason_v` shows the new rows in the
old shape for one release. `provledger reasons reclass-status` prints the
state.

## 6 · Honest boundaries

- The system can record that something was **shown** (a reason listed in a
  checklist or a page) and that something was **adopted** (a reason pointed
  at as `because`, next phase). It cannot record that anyone **read** it,
  and it will not pretend a display count is a reading.
- *Recorded at X* is not *happened at Y*: `occurred_at` is what a person
  claims, `recorded_at` is what the database saw. Both are kept; neither
  overwrites the other.
- A stated reason is exactly a span of a recorded utterance. If the words
  were never recorded, the best the system can say is `asserted`.
- The rules are deterministic and their basis is written down; when none
  applies the system asks, and when nobody answers it records `unstated`
  rather than inventing.

## 7 · Cost

Every tool call is one `tool_call_log` row; `provledger metrics plan <id>`
and `metrics baseline --write docs/perf-baseline.json` turn that into per-plan
numbers, and `test_h1_threshold` fails when the last measured plans exceed
1.5 × the baseline's p90 calls per step.
