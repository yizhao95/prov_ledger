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
  checklist, a page, a headline or a hook's injection — `read_hit`) and that
  something was **adopted** (a reason cited by id — `influence`, phase 2). It cannot record that anyone **read** it,
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

## 8 · Phase 2 — the value points: shown, adopted, asked

Phase 0/1 recorded reasons; phase 2 is where they are taken out again. Four
places, and only four (NORTH-STAR: 不过度干涉):

| moment | what happens | what is written |
|---|---|---|
| **publish** | `context_pack.build` — one bounded read per target (identity chain, constraints, rejected paths, reasons, prior claims, the card) trimmed to a token budget in a fixed order (reasons → rejected paths → neighbour constraints → constraints), cuts shown as counts; `checks.headline` — the two-layer check printed as the plan headline, stored as a new `headline` row every time | `read_hit(moment='plan')` per record shown |
| **edit** (PreToolUse: Edit / Write / MultiEdit) | the active constraints anchored on the lines about to change and one hop downstream, injected as `additionalContext` (statements only, ≤ 600 chars, ending with `provledger why <qn>`); not one byte when nothing anchors there; nothing opened for a file outside every registered repo | `read_hit(moment='edit', injected_chars)` |
| **close** | rules R0–R6, the unstated backstop, `close_headline` (proceeded / unanswered blocking findings → survival expectations) | `change_reason`, `expectations` |
| **why** (`provledger why <qn|nk_…|file:line>`) | the same pack, printed: `node · 下游 n · 履历 m 次 · 约束 k（生效 j）· 否决 r · 待补 p`, then each record as `#id · tier · 来源等级 · when · 展示 n 次 · 被 <plan> 采用`; `--impact`, `--all`, `--pending`, `--never-read` (constraints nobody was ever shown), `--search` (FTS5, LIKE when unavailable and it says so) | `read_hit(moment='why')` |

**Adopted** is written by exactly three paths, all of which cite a record id: a
headline response (`headline-respond`, `provledger headline respond|ack`), a
reason's `because`, an acknowledged constraint. `influence` and `read_hit` are
never derived from each other (I11); `reason_stats_v` reports shown per moment
and never sums across moments — the edit hook can show a hot file's constraint
dozens of times a day.

**Never blocking.** `publish` exits 0 whatever the headline says; PreToolUse
touches `permissionDecision` only for a human constraint declared `block: true`
in the extensions file, which is also the one exit-5 path of publish. R0 makes
the user's own sentence naming a node its stated reason before any other rule.

### 8.1 · Cost — what provledger costs a plan

`tool_call_log.command_head` (the first 80 chars of a Bash command) lets
`plan_metrics.overhead` bucket a plan's calls into *orchestration*
(`run-step | publish-plan | review_run | complete-step | …`) and *provenance*
(`reason- | provledger | hooks/ | analyzer`); `overhead_ratio` is their sum,
`context_overhead_tokens` = the pack's `approx_tokens` + the headline text / 4 +
every PreToolUse injection in the window / 4. `test_h1_overhead_ratio`
(provenance ≤ 10 %, overhead ≤ 35 %) and `test_h4_context_overhead` (≤ 3000)
run over the last five completed plans and **skip out loud** when there is
nothing to measure. `docs/perf-baseline.json` carries the two columns and a
hand-measured `superpowers_only` reference (a headless session with the
provledger plugin disabled, tool_use count and input tokens read from the
stream-json transcript — a reference number, never an assertion: a single run
jitters 20–30 %).

### 8.2 · Degraded mode — hooks alone

Without the skills nobody publishes a plan. The Stop hook then records the
session (`session_run`, the one table that may be UPDATEd) and, when the repo
is registered, the session published no plan, the tree moved since the graph
was built, `reasons.session_refresh` is not `off` and no refresh is running,
queues `init_project.sh --trigger session --session-id <sid> --notify-orch-db`
in the background. When it reports back, R0–R6 run over the session's changed
nodes under the placeholder plan `session:<sid>`, the rest is backstopped
`unstated`, and a headline is stored for the session — never printed.

What is lost is explicit, never silent (`test_degraded_mode.py`, marker
`degraded`):

| with the skills | hooks only |
|---|---|
| a plan with steps, deviations, a review | `session_run` with `refresh_state ∈ skipped/queued/done/failed` and the reason |
| reasons answered at close (stated / asserted / unstated) | only `derived` (rules), `stated` (R0) and `unstated` — never `asserted`: nobody interpreted anything |
| headline printed and answered; `influence` rows | `headline(session_id)` stored, `headline_response` and `influence` empty |
| records shown at plan / close | only the edit hook's injection is `read_hit` |

`selfcheck` reports `sessions_without_plan` and their unstated share.

### 8.3 · Honest boundaries, continued

- *Shown* is a `read_hit`; *adopted* is an `influence`; nothing is *read*.
- The headline is computed from what is on record: a node with no history has
  no findings and says so (`0 findings`), which is a statement about the
  ledger, not about the change.
- The edit hook resolves lines through the last analysed snapshot; edits below
  the last refresh's line numbers can miss or mis-anchor. It says which node it
  matched, and `provledger why file:line` shows the same resolution.
