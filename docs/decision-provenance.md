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

## 9 · Phase 2b — three views on one context triple, significance, R0 sees the words that caused the plan

### 9.1 · The context triple `(project, node, at)`

Every view carries the same three things in its URL and hands them to the
next view unchanged (I12): `/graph/{project}?focus=<qn|nk_>&at=<run>`,
`/node/{project}/{qn}?at=<run|reason_id>`, `/plan/{plan_id}?node=&at=`. The
fixed bar at the top — **Graph · Node · Task** — is built from the triple by
`queries.view_bar`; the current view is highlighted, a view with nothing to
anchor to is disabled rather than a dead link, and the breadcrumb reads
`project › node › at`. Missing items degrade: no `at` means the latest run
or record; no `node` means the Task view highlights nothing; no state graph
means Graph and Node say `state graph unavailable` and the bar stays.

| view | answers | what it shows |
|---|---|---|
| **Graph** `/graph/{project}` | what does the project look like now (or at run N), which nodes have a story | nodes of `node_snapshot` at the run (functions / methods / routes by default, `level=full` for everything), a **badge** per node = `node_badge_v` (reasons whose effective significance is not minor + rejected paths + active constraints), the colour of the latest event's tier; the graph keeps no edge history, so a historical run shows today's edges mapped by node_key and says `edges_from: latest`; vis-network from the CDN with the full node table underneath, so an offline page still answers |
| **Node** `/node/{project}/{qn}` | what happened to this node at each moment, who was shown it, who used it | the timeline (the run at `at` highlighted), every record with 展示 per moment and 被 <plan> 采用 links that carry the triple back into the Task view |
| **Task** `/plan/{plan_id}` | what did this task read, what did it decide | the headline, the per-step 展示过 / 采用了 columns, the focused node highlighted, the session it was published from and that session's other plans |
| **Session** `/session/{id}` | what was said, what it cost, what changed — with or without a plan | utterances (a personal one marked), tool calls in the two buckets, the session refresh's nodes, the never-printed session headline, the plans; `降级（无 plan）` when there were none |

### 9.2 · Significance — a hint on every reason, a verdict on the record

The computation layer never changes: `node_event` records everything. The
threshold only shapes what is shown and asked about. At review close every
new reason gets a **hint** (derived): the user's own words (tier stated), a
struct_sig change, ≥ 1 downstream consumer, a gate failure in the plan, an
active constraint on the node, a recorded outcome — any one → `major`, none →
`minor`. Each hint is one `significance_log` row with its basis. With
`reasons.significance: llm` in `provledger-extensions.json` the arbiter's
headless runner is asked for a **verdict** too — strict JSON only; a
non-JSON answer leaves the hint standing and the row says so. A person's
word is `provledger reason mark <id> major|minor` (judged_by human).
`change_reason_v.significance_eff` = the latest verdict, else the latest
hint, else NULL; the pack, `why` and the views fold only an explicit `minor`
into a count. `provledger significance eval` (manual, never CI) prints the
hint × verdict confusion matrix; selfcheck lists `significance_disagreements`
(hint major, verdict minor) — believed, but on the record (I14).

### 9.3 · R0 sees the words that caused the plan

Phase 2 ended with zero real R0 hits for two reasons that were in the design.
The sentence that causes a plan is said **before** the plan exists, so the
candidate window never saw it (FL-069): a plan now carries `Plans.session_id`
— the newest tool call from this repo within 30 minutes, else the session id
the hooks-less path leaves in the environment, else NULL and one stderr line
— and everything said in that session is a candidate. A file name in a
sentence matched every node in the file and was judged generic (FL-066): the
basename is its own rule now and anchors to `psg_bridge.changed_in_file` —
the nodes the plan changed in that file, all of them, never the untouched
ones, outside the "> 3 nodes" limit. The local-name rule (≥ 4 characters,
stopwords, > 3 nodes per sentence = generic) is unchanged: 挂错比不挂糟.

### 9.4 · Honest boundaries, continued

- A badge counts records, not importance: a node with three minor reasons
  and one active constraint shows `1`.
- The Graph view's edges are today's; the page says so whenever `at` is not
  the latest run.
- The session that installs the hooks does not fire them; the phase-2b
  dogfood ran in such a session, so its cost numbers are the stored packs
  only and its R0 material entered through `provledger note --session`.

### 10.6 · `/ledger` as a slash command (phase 2e, Task 6)

The dashboard page is the second entry point. The first is a slash command in
the session you are already working in — `/ledger why is our train/test split
80/20?` — because the question usually arrives *while* you are changing the
thing, and because there is already a model in the room:

```
a.  provledger ask "<question>" --json --no-model     # code locates + computes
b.  the SESSION's model drafts ≤ 8 sentences, each ending in [#id] or [scope]
c.  provledger ask submit <ask_id> --answer-file <f>  # code checks and records
d.  the checked answer, the scope line, the cited records, two follow-up reads
```

`--no-model` in step (a) is deliberate: spawning a second, headless model to
answer a question the session's model can answer is a round trip nobody asked
for. What must not change is step (c): the draft goes back through the same
`summarize.review` the headless path uses — same J1, same J2, same counted
deletions. **A draft that is never checked is a model talking to itself**, and
it does not matter which model wrote it.

`ask_log` is append-only, so a second draft is not an update: `ask_answer(ask_id,
version, answer, cites, dropped, model)` (migration 024) keeps every version,
including the ones that were rewritten. `model='session'` distinguishes them
from the headless runner's.

`skills/ledger/SKILL.md` states the two commands the skill may run and nothing
else — no edits, no other tools, no answering from memory or from the source
tree. A read that can edit is not a read, and the rule only holds if it is
written where the model reads it; `tests/test_skill_bundle.py` asserts it stays
written.

## 11 · Phase 2c — declaring the world outside the code

The third core (NORTH-STAR): *a user says one sentence and the meeting
decision, the external system, the hand-computed figure enters the graph, and
from then on it has exactly the same provenance as a function.*

### 11.1 The table (migration 026, `declared_node`)

Append-only like `utterance` / `reference` / `change_reason`: one INSERT per
version, `superseded_by` is the only column an UPDATE may touch, DELETE is
refused, and every row carries the sha256 chain `provenance.verify_chain` walks.
One column carries the whole design:

```sql
tier TEXT NOT NULL CHECK (tier IN ('stated', 'asserted'))
```

`observed` is absent on purpose. Nobody observed a steering-group decision:
somebody said it, or a model tidied up what somebody said.

`node_type` is one of `external_system`, `business_rule`,
`stakeholder_decision`, `external_dataset`, `manual_figure`. `§9`'s
`manual_figure` is one of them; `business_rule` is the node form of a
constraint.

### 11.2 Who said what decides the tier

`declared.declare()` has **no tier parameter**, and the rule is mechanical:

| what | tier |
|---|---|
| a field you typed (`--type`, `--attr`, `--links-to`) | `stated` |
| a field the model tidied out of your sentence | `asserted` |
| the ROW, once you confirm it with your own words | `stated` |
| the edges the model proposed, after that confirmation | still `asserted` |

The last row is the point. `field_tiers_json` keeps the per-field answer next to
the row-level one, so confirming a draft never rewrites who wrote which part.
The confirming sentence is recorded as an `utterance`, and the row points at it
— which is what gives `stated` something to be true about.

The model's whole job is turning one sentence into
`{node_type, name, attrs, links}`. It may not name a node that is not already in
the graph, and **one bad name discards the entire answer**, naming what was
wrong: a partly-believed tidy-up is worse than none. The runner is injected
(headless `claude -p --tools ""`), so tests never call a model.

### 11.3 In the graph, computed like everything else

The analyzer's declared stage reads the project's ACTIVE declarations and writes
one node plus its `declared_feeds` / `declared_constrains` / `declared_depends_on`
edges; a link whose target is missing or ambiguous is counted, never guessed.
`provledger.declared` — the third built-in provider — projects those rows, and
the host does the rest. Identity in three layers:

| layer | what it is | so |
|---|---|---|
| qualname | `declared:<slug>` | revising a declaration is the same node; declaring a second one is a new node (§18: we never judge whether two descriptions mean the same object) |
| struct | the declaration's attributes | edit them → the next run computes `node_changed` |
| dataflow | the links, *and whether each target is still in the graph* | delete the function a rule constrains → the rule's footing moved, and it is recorded |

Retiring a declaration appends a `retired` row; the next run computes
`node_removed`. Because `declared_node` is append-only, the retired row still
names what it pointed at — so a retired declaration DOES appear as
`removed_upstream` of the nodes it constrained. That is the half of FL-083 this
phase can close honestly; the code half (who called a deleted function) still
needs the previous run's card.

The provider passes all six conformance contracts over a graph built from the
mutation corpus, so `rename_function`, `delete_function` and `move_file`
actually reach the declared node and "preserved for every mutation" is tested
rather than skipped past.

### 11.4 A rule is a node AND a reason

Confirming a `business_rule` or a `stakeholder_decision` that constrains
something also writes one **active constraint** per target, carrying the same
words as its span. So the rule is a node you can point at in the graph and a
record the next plan's heads-up finds — which is what makes the walkthrough's
last step work: one sentence, said once, turns up as a blocking finding in the
next plan that touches what it governs. An `external_system` anchors nothing: it
is a fact, not a rule.

### 11.5 In the three views

Node: a `declared` marker with the declared type and the DECLARATION's tier,
printed **beside** the event tier the graph computed — they answer different
questions. Graph: external systems, datasets and hand-computed figures sit at
level 0 (they are where things come from); rules and decisions get a constraint
lane, excluded from the topological sort, drawn as a panel that links each rule
to what it governs — a rule must not push the code it governs down a level, or
the picture claims the rule produces it.

### 11.6 What this deliberately does not do

No extraction of nodes from documents. No judging whether two descriptions mean
the same object: changing a description is an attribute change on the same node,
a new `declare` is a new node. The model never produces a result number and
never labels its own output as the user's words.
