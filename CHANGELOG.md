# Changelog

All notable changes to provLedger — the `provledger` package, the six
skills (`writing-plans`, `executing-plans`, `project-state-graph`,
`update-project-state-graph`, and the two question surfaces `ledger` and
`receipts`) and the read-only dashboard. Dates are the
merge dates of the phase PRs. FL-nnn is an entry in the project's internal
deferred-work ledger, which is not published; the part of it that affects
users is written up in [`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md).

## Unreleased

### Added

- **A plan says which root cause it serves.** A plan is one task; why it exists
  usually started earlier, in the user's own words, and several plans may carry
  the same root forward. The plan-input takes an optional `root`:
  `{"kind": "new"}` when this task starts one (optionally pointing at the user's
  recorded sentence), or `{"kind": "continues", "plan_id": …}` when it carries an
  earlier plan's root on. Without it the root is recorded as unknown, and
  publish lists the project's recent roots after the headline. Every judgement is
  a new row in an append-only table, recorded as `asserted`; continuing a plan
  resolves to where its root started.
- **The headline shows the earlier tasks under the same root.** A plan that
  continues a root gets a third layer: each earlier task under it, and each
  rejected path those tasks recorded, which can be answered (and so adopted)
  like any other finding.

## 0.4.6 — 2026-10-09

### Fixed

- **A `stated` reason quotes only what the user typed.** Claude Code sends some of
  its own text through the prompt hook: a finished subagent's report, a task
  notification, a system reminder. Older hooks recorded some of it as the user's
  words. Those rows stay in the ledger, but the close-time rule that quotes the
  user's words still read them, so a sentence from a subagent's report could
  become a node's `stated` reason. The rule now leaves that text out, and the
  ledger refuses to quote it as `stated` whoever asks. The hook and the rule use
  one definition of injected text.
- **A rejected path hangs on the node its failure names, or on the plan.** The
  close-time rule that records a failed step as a rejected path used to pick the
  first node whose name was any word of the failure text, so "run the suites"
  landed on a function called `run`, and `why` told that function's story with
  someone else's failure. It now anchors only on a node the plan changed and the
  text names as code (`Module.func`, `func(`, `` `func` ``, or a snake_case or
  camelCase name), and otherwise on the plan. Ordinary words never anchor.
- **A corrected record reads as its correction.** A correction is appended and
  supersedes the row it corrects; the old row keeps its words. `why`, the
  `/ledger` and `/receipts` fact table, the plan-time context, the export and
  the dashboard now show the newest row of each chain and say which row it
  corrects (`corrects #N`); `provledger record #N` still prints the old one. The
  node badge counts a chain once.
- **`provledger reasons recheck`** lists the rows the two rules above wrote
  before this release: `stated` reasons quoting injected text, and rejected
  paths on a node their text does not name. `--apply` appends a correction for
  each (an `unstated` row, or the same rejected path re-anchored) in one
  transaction. Run it once after upgrading; without `--apply` it writes nothing.

### Development

- **A test that opens the real ledger fails its suite even when it adds no row.**
  One backend test ran `provledger trigger eval` without its own `ORCH_DB`, so it
  opened `~/skill-workspace/orchestrator.db` with the working tree's code: that
  switched the ledger to WAL and ran the branch's migrations on it before any
  release carried them, while every row count stayed the same. The test now uses
  a ledger of its own, and `scripts/home_guard.py` also compares the real ledger's
  applied migrations and journal mode before and after each suite.

## 0.4.5 — 2026-10-07

### Fixed

- **A failed publish leaves nothing behind, and a plan nobody started can be put
  down.** A step type the orchestrator does not know, expectations on a plan
  without a tracked project, or a headline note citing a record that does not
  exist were found only after the plan row was written, leaving a plan
  IN_PROGRESS with no steps that nothing could close. Every check now runs
  before the first write, and the plan, its steps and its skills are written
  together. `abandon-plan.sh` (plan id, reason) turns a plan whose steps never
  started into the new status ABANDONED, with the reason on the plan's record.
- **A COMMAND step is never left stuck.** `start-step` refuses a COMMAND step —
  only `run-step.sh` starts one, because only its exit code completes it — and
  `run-step` stopped by a timeout or Ctrl-C fails its step and says it was
  stopped, instead of dying with the step IN_PROGRESS.
- **A project registered outside the default directory keeps its graph.** The
  review's refresh and the session refresh wrote a new graph, with no history,
  into the default directory, and the registry followed it; both now refresh the
  graph where the project is registered.
- **A plan whose failed retry was retried in turn closes.** A plan that failed
  through a regular step is judged again once every failure is recovered. When a
  retry had itself failed and been retried (the retry a child of the attempt, as
  the executing-plans skill says), that judging stopped with
  `'sqlite3.Row' object has no attribute 'get'` and the plan stayed FAILED.
- **A plan close takes seconds, not minutes, and finishes.** On a large project the
  close's rules read the state graph without an index — every lookup of a node's
  latest snapshot scanned the whole table — and recomputed the plan's changed
  nodes once per file a sentence named. On provLedger's own graph a close took
  1 to 10 minutes; the review's 60 s ceiling cut every one, rolled it back, and
  the plan was closed by hand without its rules ever writing a reason. The graph
  now has the two indexes (created by the review's refresh, before the close),
  the changed nodes are computed once, and the rule that matches node names
  against the user's words looks them up instead of running one regex per name:
  the same closes now take 1-2 s with identical results. When a write still
  outlives its ceiling, the review driver reads the step and the plan back and
  says what they show.
- **The hooks stop losing the user's words to a locked ledger.** The ledger now
  runs in WAL, so the dashboard's poll, a long read or a plan close no longer
  holds a hook's write up, and a prompt or a tool call that cannot get the write
  lock within two seconds goes to a spool file beside the ledger and is written,
  with the time it happened, by the next hook. Before, it was logged to
  `hook-errors.log` and gone. (The Stop hook's row is not spooled yet.)
- **The provenance hash chains cannot fork under two writers.** A chained insert
  read the chain head before taking the write lock, so a second writer could slip
  in between and both rows would point at the same predecessor — which `verify`
  reports as tampering. The head is now read under the write lock, and compound
  writes take the lock when they open.
- **Before the first plan, the dashboard shows the empty state, not an error.**
  With no ledger yet the page rendered a red "orchestrator.db not found" box,
  although the launcher says the dashboard is running; it now shows the empty
  state naming the command that publishes a plan. Found by the release check.
- **`ensure-dashboard.sh` probes the port the dashboard starts on.** Setting
  `PROVLEDGER_DASH_PORT` alone started the dashboard there and then waited on
  8765 — or, with another dashboard already on 8765, reported that one as up.

### Changed

- **`/ledger` answers as a handoff, and `/receipts` says what to check before you
  send.** Both skills are rewritten from Anthropic's public guidance on skills and
  prompts: the intent first, every rule with its reason, a template and complete
  examples. `/ledger` leads with the answer, then where it came from (who asked,
  when, in their own words), what was done about it, and what holds now, in date
  order. It checks every sentence against what this session read before
  submitting, never shows a sentence the check deleted, and may resubmit once
  with an id it found. `/receipts` writes a complete reply told as a timeline and
  lists, apart from it, what the record does not settle — for you to confirm or
  add before sending. `/ledger` is for you, so it may go into detail: `ask
  submit` now keeps up to 15 sentences, not 8. `/receipts` speaks for you to
  someone else, so its reply is official, keeps to their question and holds
  nothing uncertain. Both copy every number and identifier from a read, and the
  scope line as each read printed it.
- **The fact table says what an agent used to guess.** A node whose reason was
  closed `unstated` now shows a cited line ("nobody said why this changed") and
  the absence "No reason was recorded for X." — before, it printed `reasons (0)`,
  which reads like "never touched", and an agent supplied the goal of the task
  that changed it. Every source says it is a link only, that the body is not in
  the ledger, and whether anyone checked it. The table, and the `/receipts`
  material, open with a legend: what each tier and cite token is, and how to put
  it into words.

### Development

- **The release check walks a plugin user's path** (`scripts/release-e2e.sh`,
  INSTALL.md §5): a new stage 0 installs the plugin into a sandbox and runs a cold
  first session and a first task the agent plans itself; stage 2 also asks every
  question as a real `/ledger` or `/receipts` slash command; stage 3 marks those
  real answers, and a disagreement is a finding a person decides.
  Its model calls copy the developer's current login right before each call, and
  a call that could outlive the login is not made and reads as BLOCKED: a sandbox
  that refreshed the login would leave the developer's own unable to refresh. The
  judge is handed the whole material an answer was written from; it used to be cut
  at 24000 characters without a word, and a quoted line past the cut was marked as
  resting on no record.
- Measured on real sessions before release: the dummy project's twelve questions,
  asked twice of the old skills and of the new ones and marked by a model judge.
  Answers told in date order went from 9 to 21 of 24, questions whose two runs
  disagreed from 5 to 1, answers with a claim resting on no record from 5 to 3;
  key points hit stayed at 43 of 48. (`scripts/release_e2e/skill_ab.py` runs that
  comparison for any two refs.)

## 0.4.4 — 2026-10-05

### Fixed

- **`/ledger` and `/receipts` could not answer for a plugin user.** Both call
  `provledger` by name, and the command lives in the plugin's venv, which nothing
  put on the session's PATH: the first command failed with `command not found`.
  The plugin now ships `bin/provledger`, which Claude Code puts on the Bash tool's
  PATH while the plugin is enabled, and which hands every call to the venv. Found by
  a sandboxed install with a real headless session, the first time the plugin path
  itself was exercised. claude.ai and Cowork do not install a plugin with a
  top-level `bin/`; install with the Claude Code CLI.
- **A constraint added to a new ledger was recorded twice.** A ledger constraint is
  mirrored into the readers' table when it is added, and the one-time reclass that
  every ledger runs on a later open copied it again — so on any ledger created
  since decision provenance, a constraint added before that first reclass became
  two. Readers folded the twins only when both writes fell in the same second;
  under load they did not, and a plan close recorded the bypass of one constraint
  twice. The reclass now leaves an already-mirrored constraint alone. Found as a
  scenario that failed about one run in fifteen under load.
- **`/receipts` could say more than a record says.** A source is a pointer — the
  ledger holds its label, time and link, never its body — and the skill now says so
  in its hard rules: never call a linked email attached, never offer to send it,
  and never present an asserted record as something anyone agreed. Found by the
  release check's judge.
- **Failed tool calls were never logged.** A failed call fires `PostToolUseFailure`,
  not `PostToolUse`, and only the latter was hooked. Both are now, and
  `tool_call_log.failed` (migration 032) marks the failures.

### Development

- The repository's own `.venv` is the development venv; `run_tests.sh` prefers it
  to the plugin's venv, which now holds the installed release (`--which` prints the
  choice). The home guard ignores SQLite's transient sidecar files.

## 0.4.3 — 2026-10-04

**A clean-up of the development setup, and the defects it turned up.** A fresh read of
the repository, with the documents checked against the code, found claims that had
drifted, paths only a new user takes that had never run, and seams between two
scripts that each passed their own tests.

### Fixed

- **Words the user never said were recorded as theirs.** Two kinds of text reached
  the prompt hook and became `utterance` rows — the source of the `stated` tier: a
  subagent's report handed back to the session (`<agent-message …>`), and the
  prompts provLedger itself sends to a headless `claude -p` (the judge, the arbiter,
  `ask`'s summary), because the user's plugins run in that child too. The hook now
  skips the first, and the runner marks its child with `PROVLEDGER_HEADLESS=1`,
  under which every provLedger hook stands down. Rows already written stay: the
  tables are append-only.
- **`run-step.sh` swallowed the review hand-off.** It sent complete-step's output to
  `/dev/null`, so `needs_agent_review` — the signal that a plan on a registered
  project must stop for a review sub-agent — never reached the caller. Its last line
  now carries complete-step's and fail-step's signals.
- **The external-artifact judge broke on the real runner.** It handed the runner's
  `(text, detail)` pair to the parser and raised `TypeError`; with the switch on,
  that rolled back the whole plan close. The judge now reads the text, and a judge or
  runner that fails records a `silent` verdict instead of rolling anything back.
- **The declaration runner inherited your language setting.** It was a second copy
  of the model runner without `--settings`; it now uses the shared one.
- **Tests and the demo could rewrite your real graph index.** With only
  `PSG_REGISTRY_PATH` set, `init_project.sh` still wrote
  `~/skill-workspace/project-graphs/PROJECT-STATE-GRAPHS.md`, dropping your real
  projects from it. The index and the default out-dir now live beside that registry.
  The provenance demo no longer takes an `ORCH_DB` or `PSG_*` path from the caller's
  shell either: `DEMO_HOME` is its only knob, so a shell pointing `ORCH_DB` at your
  real ledger cannot receive the demo's rows.
- **`bootstrap.sh` never upgraded the backend, and failed from a project directory.**
  Its marker hashed only `requirements.txt`, so a version bump never re-installed the
  editable backend (no `provledger` command, 0.1.0 metadata); and uv resolved
  `-e ./orchestrator-backend` against the session's working directory. It now
  re-installs when the backend's pyproject changes, and installs from the plugin root.
- **The dashboard:** `?lang=zh` survives the 2 s poll and the view bar (the poll's
  ETag now varies by language); `launch_dashboard.sh` reports a dashboard with no
  ledger yet as running instead of failing and then reporting "port in use", and
  `ensure-dashboard.sh` agrees; the empty state names `publish-plan.sh` instead of
  the broken `orchestrator-cli.py`; the design bundle is rebuilt and no longer carries
  the old Chinese labels; the webapp's pyproject declares `httpx`.
- `orchestrator-cli.py` runs again; the analyzer no longer walks `.venv-*` directories.

### Development

- **`scripts/run_tests.sh`** runs, counts (`--count`) or lists the suites, each in its
  own pytest process, from the one list in `scripts/suites.sh` (the release check
  reads it too). It replaces `count_tests.sh`, and fails any suite that writes to the
  real `~/skill-workspace` (`scripts/home_guard.py`).
- Copies are held to their originals by tests: the plan and update-input schemas to
  the code that validates them, SKILL.md descriptions to the 1024-character limit, the
  analyzer's `graph_viz.py` to its copy, the selfcheck table to `selfcheck.py`, and the
  version string to `scripts/pkg_smoke_test.py`.
- `CLAUDE.md` files at the root and in each component hold the development
  conventions.

### Documents

- The README is a front page again. The command reference moved to
  [`docs/cli.md`](docs/cli.md), the dashboard's routes to
  [`orchestrator-webapp/README.md`](orchestrator-webapp/README.md), and the five
  quick-start commands were re-run: two of them did not print what the page said.
- Shipped documents no longer carry a worked case from the local answer-quality
  evaluation (a walkthrough in the README, anecdotes in skills and docstrings).
- INSTALL, KNOWN-ISSUES, the skills' SKILL.md files and `docs/decision-provenance.md`
  were corrected where they disagreed with the code: stale paths and counts, tools
  that exist only in another agent harness, the hash chains `verify` walks, and the
  dashboard's one write.

## 0.4.2 — 2026-09-30

**The ledger held the answer and nothing could read it.** Asked why a review timeout
is 4800 seconds — a decision this project recorded in full — three separate searches
reported "not recorded", because the derivation sat in a step's `log_context` and no
command read that column. This release is the descent path from a record to the task
behind it, plus the four reads that path needs.

### The reads

- **`provledger plan <id>`** — a task's steps in tree order with their failures and
  logs (`--step`, `--full`, `--log-chars`). The only read that reaches a failure
  inside a plan whose status is `COMPLETED` *because the failure was recovered*: the
  detour is gone from the status and these rows are the only thing that remembers it.
- **`provledger record '#12'`** — one record whole. Every other read bounds record
  text at 240 characters, and did so **mid-word with no marker**, so a reader could
  not tell they had a fifth of a row. Measured: one record is 1358 characters and
  `why` showed 240. Bounded reads now say what they cut and name the read that has
  the rest.
- **`provledger graph [<target>] --depth N`** — the project graph folded to areas with
  how many nodes carry records, unfolding to neighbour **names** with their edge type.
  Project node names alone run to ~239k tokens, so the view is paginated; the fold
  states its size and the command that lifts it, and it is never a judgement about
  relevance.
- **`why --all` and `--impact` now work.** Both were no-ops that recommended
  themselves: `--all` printed "3 more reasons not expanded — pass `--all`" to a
  caller who had passed `--all`, and `--impact` printed caller counts and no names.
  A third defect in the same place had the blast-radius line and the footer
  contradicting each other in one output (`callers 12` / `callers 38`; 50 was true).

### Answering

- **An unsupported number is named, not deleted.** The answer check used to remove any
  sentence containing a number its fact table did not state. The machine can tell
  whether a number is in its table, not whether it is true — and a figure read out of
  a step log is sourced yet absent from the table, so the rule was deleting correct
  answers. It now reports every such number and keeps the sentence, on the terminal
  and on the `/ledger` page alike. The language check was changed for this same reason
  first; numbers were the case it missed.
- **`/receipts` is a skill**, alongside `/ledger`. It answers a colleague who
  questioned a decision: the reply first, the evidence under it with a record id per
  line, and a closing question about tone. It never sends anything and writes nothing.
- **`receipts` split into two reads.** `receipts candidates "<what they said>"` offers
  starting points and says outright that the score orders the list and does not choose
  it; `receipts facts <node>…` is the timeline, the gaps and the range for the nodes
  you picked. The old one-shot form still works.
- **Graph bookkeeping is out of the challenge material.** On a real question, 34 of 63
  timeline lines were `the graph recorded node_changed in run N` — the graph's record
  of itself, not testimony about a decision. They are counted and named as left out.
- **The CLI no longer starts a second model.** `--runner`'s default was `claude`, so a
  bare `provledger ask` shelled out to a headless model with no session, no tools and
  no context. The session already has a model; the default is now `none`, two tests
  booby-trap every entry point, and `--runner claude` is unchanged.

### Corrections

- **A deviation is printed `#v94`, not `#94`.** Deviations and ledger records are
  numbered from separate sequences and both printed as `#N`, so `record '#94'` on a
  deviation returned an unrelated record from another plan — no error. A silent wrong
  answer in the one place this tool has to be checkable. `record` now refuses a `#v`
  cite and names the read that holds it.
- **Reading the code is encouraged.** The skills said "never answer from the
  repository", written to stop code being used as *evidence*, which also forbade using
  it as an *index* — and grepping for the thing you are asking about is how anyone
  starts. provLedger supplements the code: code shows the winner and never the
  rejected option, who asked, or what it cost. The one rule is: do not read the code
  and invent a reason, because the real one is usually recorded.
- `INSTALL.md`: the clone URL was ssh; §4's `pip install` fails in §3's Option B venv,
  which has no pip, and neither section said so; nothing installed the `provledger`
  command the README prints, so the dashboard's graph and `/ledger` pages rendered
  "unavailable" rather than failing loudly. All four are fixed, and §4a installs it.

### What was recorded and could not be surfaced

The release sandbox builds a project whose whole history it wrote, then asks
eleven questions and has a model mark the answers against key points fixed in
advance. Three failed, and each was a record that existed and had no path back.
None was a retrieval algorithm being insufficiently clever; each was a missing
length of wire, and two of the three were found twice because the first fix
stopped one layer short of the material.

- **`ledger-add`'s two most important kinds were unreadable.** A new entry was
  mirrored into `change_reason`, where the readers moved, but only
  `if kind == "constraint"`. So every `decision` and every `anti_pattern` added
  after the one-time backfill landed in a table no answer path reads — and those
  two are the whole reason that ledger exists. Each is mirrored now as what it
  is: an anti-pattern is a path tried and failed, which is `rejected_path`; a
  decision is a `reason`. Mirroring them as constraints would have misreported
  them.
- **A record hid the words it quoted.** The quoted span was reachable only as the
  last resort of a `COALESCE`, so any record carrying a paraphrase showed the
  paraphrase and said nothing about having the words. That is backwards for a
  `stated` record, whose entire claim is that the user's own words say it — and
  `node declare --confirm` writes both, so the decider and date a declaration was
  confirmed with were invisible. They travel in `quoted` now, beside `text` and
  marked as a quotation, bounded and saying how much was cut.
- **A metric could not be chosen, so nothing recorded about one could be
  reached.** `facts` matches metrics by name and metric-targeted expectations by
  target against the names `locate` returns, and `locate` had no matcher over
  metrics at all. Metrics a question names are offered now, matched on two parts
  of the name — loose enough to ask in your own words, strict enough that
  `discount_rate_pct` does not answer every question containing "rate".
- **An outcome's own account of itself was dropped in the renderer**, leaving
  `{"drift": 6.6}` as the whole story of a claim that turned out false. A reader
  cannot see from a JSON blob that an estimate missed by 6.6 points, and that
  sentence is the entire value of having recorded the outcome.

Every layer's tests asked "did this layer do its job", and all 1144 of them
passed while these four paths were broken. None asked whether a person who asks
the question gets the sentence. That is what the sandbox asks, and it is why each
fix here was verified by rendering the real material and grepping for the words,
not by a green unit test.

### Before every release

`scripts/release-e2e.sh` — a clean sandbox that clones, follows `INSTALL.md` as
written, builds a dummy project, exercises both question surfaces, drives the
dashboard in a real browser, and has a model mark the answers against key points
written in advance. Every place it must deviate from the document is a finding,
because that is where a new reader gets stuck.

## 0.4.1 — 2026-09-30

A fix release. The dashboard defect in it is the kind this project is supposed to
catch, and it took clicking the thing to find.

- **The dashboard did nothing when clicked, on Chrome and Edge only.** A handler
  cancelled htmx's own swap and then called `htmx.swap()` to do the replacement
  itself — an API that arrived in htmx 2.x, while the page loads 1.9.10. So the
  default was switched off and nothing took its place. Firefox and Safari were
  unaffected because they returned earlier, which made it look like an
  environment problem. Cancelling a default is now guarded on the replacement
  actually existing. Records and the pre-change check were never affected: the
  dashboard opens the ledger read-only and the write paths never touch the
  browser layer.
- **A ninth test suite existed and had never been run.** The repository root's
  `tests/` — 16 tests covering bootstrap, the plugin manifest, the marketplace
  entry, bundled skills and runtime dependencies — was missing from all three
  places the suite list lives, so every count from 1826 onward was 16 short. It
  is in the list now, and the one assertion it failed on had pinned a single
  spelling of the install command while the README had moved to the other.
- The bilingual source-mention word list is on the language whitelist, where
  `vocab.py` already sits: a list that only knew English would silently never
  fire for half of what gets said.

2115 tests across nine suites.

## 0.4.0 — 2026-09-30

Evidence. A decision now carries two separate things: a **claim**, which comes from
the task, from your own words, or from nowhere; and **evidence**, which comes from
written communication and is found by your agent with your agent's own credentials.
They never move each other. Attaching an email to a reason does not change what kind
of statement that reason is — it changes how far the statement can be checked.

The rule underneath all of it: **an agent may not guess.** A reason it cannot ground
in what you said is written `unstated`, and a pointer that turns up with nobody having
said anything is attached to that blank rather than converted into a plausible reason.
A blank is a fact. A fluent guess is a liability.

### Where a claim came from
- **`utterance.origin`** (migration 030): which door the words came through — the hook
  that captured your keystrokes, a person at a terminal, an agent running the CLI.
  `recorded_by` has existed since 018, but the writer declares it itself: `provledger
  note` hardcodes `human`, so an agent running that command produced a row saying a
  person wrote it. Origin is decided by the entry point, so it is the half that can be
  checked. Rows written before 030 read `unknown` and are never backfilled — the
  append-only trigger refuses the update, so "we do not guess where old words came
  from" is enforced by the table rather than by discipline.
- The dashboard and every answer now distinguish *captured as you typed it* from
  *entered afterwards*, at the same tier and the same `recorded_by`.

### Evidence, and who goes and gets it
- **`provledger reference add`** attaches a pointer to a reason or, before a reason
  exists, to the sentence that named the source. A label, a time, a link — **never the
  body**. A test reads `cli.py` and `provenance.py` and fails if a network call appears
  in either: provLedger records pointers, your agent fetches them.
- **`reference pending` / `reference mark`** (migration 030, `reference_check`): which
  pointers nobody has opened lately, and a verdict when somebody does. A dead link does
  not disappear — it becomes "this stopped opening on this date", which is itself
  evidence. Append-only: a later check sits beside the earlier one.
- **`reference_link` gained `stance`**: evidence is allowed to contradict. An email that
  cuts against the reason is recorded as contradicting it rather than dropped, because
  silently discarding the inconvenient half is the failure this product exists to prevent.
- **A deterministic hint on `UserPromptSubmit`**: when a sentence names an outside source
  ("Sarah emailed…", and the same phrases in Chinese), one line suggests recording the
  pointer. Plain keyword matching in both languages, no model, and it never blocks. Pinning a source the moment
  you mention it costs nothing; finding it again in four months costs a lot.
- **`provledger review evidence-slots`** hands the review step a list — node, what
  changed, the plan's own time window, deterministic hints, significance — and stops.
  Your agent searches with its own tools and writes back what it found. No credential,
  mailbox or permalink body ever enters the ledger.
- **`evidence_log`** (migration 031): why a given slot is blank. "Nobody looked",
  "somebody looked and found nothing" and "the look ran out of time" are three different
  facts, and folded into one they are indistinguishable.

### Answering for a decision
- **`provledger receipts "<what they said>"`** assembles what a reply needs and stops
  there. A timeline, every line ending in its record id, outside sources with their
  links, absences stated as absences, the search range counted rather than estimated,
  and a closing instruction that every claim must map to one of those lines.
  It does **not** write the reply: composing a courteous reply is a model's native
  ability, and the material with its sources attached is the part a model does not have.
  Read-only, like `ask` — a question is not a plan.

### The witness is no longer tied to success
- **Every plan close anchors**, not only the successful ones, and the payload says which
  (`plan_status`). A plan can fail because a gate did not pass or because its own
  bookkeeping jammed, but the rows it recorded do not become untrue when it fails. What
  an anchor attests is that these rows existed at this commit and have not been altered
  — a claim about rows, not about how the work went. Anchoring only successes left the
  records most likely to be disputed as the ones with no witness.
- **`reference_check` joined the anchored chains.** In a dispute the check record is
  exactly what gets doubted — *you say you opened that link* — so it needs the same
  external witness as the words and the reasons. Payload version 2; version 1 notes stay
  readable, because a bump that made past evidence unreadable would be the opposite of
  the point.

### Fixed
- **A dry run said PASS where the real run FAILed, and never said what it had not
  looked at.** `--dry-run` covers steps 0–3; the dirty-working-tree gate is at 4b. A
  pre-flight whose whole job is to look before anything is written stayed silent about
  the gate that then auto-failed the plan. It now names every gate it did not evaluate
  — `dirty_working_tree`, `graph_refresh`, `tests`, `selfcheck`, `close_reasons`, each
  with the step it sits at — in the report and in `--json`. It checks nothing more than
  before and costs nothing more; it just stops claiming more than it checked. A
  pre-flight that does not say what it skipped is worse than no pre-flight, because the
  silence reads as a clean bill of health.
- **A review retry became a sibling of the attempt it retried, and the plan
  deadlocked.** Recovery asks whether every sub-task came through, so a FAILED
  `REVIEW.1.1` outvoted a COMPLETED `REVIEW.1.2` that had done the entire job: the
  reopen never fired and the plan could not close. Fixed where the shape was wrong
  rather than by special-casing the rollup — a retry is now a **child** of the attempt
  it retries (`REVIEW.1.1.1`), so the recursive semantics that already existed apply
  unchanged and no new concept enters the model. `--as-recovery` accepts a retry
  anywhere down that chain and refuses to run as a sibling of an attempt that has not
  recovered, naming the step to nest under instead. The cost is one depth level per
  retry: `depth_level <= 3` allows two, and a third attempt is refused loudly rather
  than quietly recreating the deadlock.
- **`review_run.py` exited 0 while reporting `{"closed": "FAILED"}`**, against its own
  documented contract. Exit 0 now means the plan closed COMPLETED and nothing else;
  completing the review step while the plan is still short of that exits 1.
- **The loop breaker offered an action the product could not perform.** At
  `revision_count == max_revisions` it said "Options: raise max_revisions, restructure
  plan, or abandon" — but `max_revisions` could only be set at publish and no script
  could change it, so the one way out it named left a hand-written `UPDATE`, which this
  project's own rules forbid. `executing-plans/scripts/raise-budget.sh` is that way out:
  `{plan_id, new_max, reason}`, validated through `orchestrator.api`, upwards only,
  `reason` required, and the raise is appended to the plan's record as a deviation
  rather than silently changing a column — a new ceiling is a decision about a plan and
  belongs in its log like any other. `revision_count` is untouched, so the raise does
  not spend the budget it grants, and the breaker's message now names the script.
- **A migration broke the hash chain, and the live ledger caught it.** Adding `origin` to
  a chained table changed what `canonical()` hashes, so all 126 existing utterances
  stopped verifying. No suite saw it — every suite starts from a fresh database. The
  verifier now accepts the pre-migration form only for a row claiming `unknown`, which
  asserts nothing: a forger can downgrade a record into worthlessness, but claiming
  `hook` requires the current form and therefore requires re-hashing every row after it.
  `verify` says how many rows predate the column rather than reporting a uniform `ok`.
- **The review step had no time limit at all.** Three `subprocess.run` calls, one of them
  running a whole test suite, none with a timeout. Now 60 / 4800 / 600 seconds, and a
  timeout kills the process group so a graph refresh cannot leave an analyzer still
  writing. The first ceiling was 300s — below every refresh this repository has on record
  (795 to 3085 seconds) — and it failed the first review it ran. The constant now comes
  from that table, and the test asserts the margin rather than the literal.
- **The overhead budgets were invisible to CI.** H1 and H4 were both `@pytest.mark.live`,
  so an ordinary test run checked the constants and never the reading path. A fixture
  ledger now drives the real path.
- **`selfcheck` counted unanchored closes only among successful reviews**, which would
  have rebuilt, one layer up, the blindness the anchor change had just removed.
- The export bundle printed `head #None` for an empty chain.

### Documentation
- README section 2 was rebuilt around what the product actually is: one view of a
  project across three axes — nodes, time and tasks — rather than four features side by
  side. Rejected paths are stated as a first-class part of the record, because a failed
  experiment leaves no trace in a diff.
- The README opening now runs a real question through the tool and shows the answer
  before making any claim about the tool.
- The PyPI gap is stated at the install step instead of at the foot of the document:
  the published package is 0.1.0 and predates all of this.

### Tests
2089 collected across the eight suites, up from 1826. Every one of the 263 added was
written before the code it covers.

## 0.3.0 — 2026-09-17

Decision provenance: why each change exists, kept next to the code, the data and
the numbers, and put in front of whoever is about to change the thing again.
The tier of a reason is decided by what it points at — your own words, a
reference, a rule the system applied, or an admitted gap — never by the writer.

### Recording the reasons
- **Verbatim capture and typed reasons** (#45): a `UserPromptSubmit` hook records
  what you actually said; `utterance` / `reference` / `change_reason` replace the
  old single reason table; free text can no longer be filed as something you
  stated. Rules R0–R6 fill in the reasons that follow from the code itself.
- **Two-layer pre-change check and the plan headline** (#46): what a plan is
  about to touch is checked against that thing's own history and against its
  blast radius; findings are recorded and shown, never blocking. `read_hit`
  records what was surfaced, `influence` records what actually changed a plan.
  `provledger why`, the `PreToolUse` hook, and a hooks-only degraded mode.
- **Three views on one anchor** (#47): Graph, Node and Task share a project /
  node / point-in-time anchor; a decision is one row with a count, never a
  repeated paragraph; significance is a computed hint with an optional, logged
  model verdict.

### Asking, declaring, proving, anchoring
- **`/ledger`** (#49): ask in words why something is the way it is, or whether an
  alternative was ever tried. Code finds the candidates, computes the facts and
  computes the absences; a model may only restate that table; then code deletes
  any sentence that cites nothing, cites an id that does not exist, or carries a
  number the table does not state — and counts every deletion. Also
  `provledger ask` and a read-only `/ledger` page with an evidence card.
- **Declared nodes** (#50): one sentence puts an external system, a business
  rule, a stakeholder decision or a hand-made figure into the same graph, with
  the same history, rules and views as code. Nothing enters until you confirm it
  in your own words.
- **Integrity and export** (#51): `provledger verify` walks the three hash
  chains and checks them against git-note anchors written when a plan closes;
  `provledger export` produces a bundle by whitelist, and personal records never
  leave — enforced in code, not by convention.
- **Numbers in decks and reports** (#52): a figure's identity is its data source,
  and a file is only where it appeared. Anchors are placed by hand, can be
  reported lost, and are never silently moved; figures with no source are
  declared as such and counted.

### Fixed along the way
- **The summarize prompt is found under the installed package name** (FL-067, again). `ask/summarize.py` asked `importlib.resources` for `orchestrator.testing`; the wheel installs the backend as `provledger`, so every installed copy raised `ModuleNotFoundError` **inside** the `try` around the model call, and `provledger ask --runner claude` printed `summary unavailable: no model` at a model that was right there. The repo suite could not see it (pytest puts `orchestrator-backend` on the path) — `scripts/pkg_smoke_test.py` now asserts the prompt loads from the wheel.
- **One note per cause.** `summary unavailable: no model` was printed for four different things. Now: `no model configured` · `no candidate nodes matched the question` · `model returned nothing (rc 3; stderr: …)` · `model call failed: <exception>` · `model call timed out after N s`, with `degraded_reason` on the doc and on the page (`data-degraded-reason`).
- **`ask_log.runner_detail`** (migration 029, append-only): per model call — outcome, the note the reader got, the command, rc, the head of stderr, wall time, prompt/answer sizes and the head of the raw answer.
- **`ask.runner`**: a runner may return `text` or `(text, detail)`; `claude_arbiter.default_runner` returns `(text, detail)` and raises `RunnerTimeout` / `RunnerError` instead of swallowing every failure into `''`; `text_runner` is the thin shim the arbiter keeps using.
- **A refusal is repeated, not discarded.** When the account's model budget ran out, `claude -p` exited 1 and printed good JSON with `is_error` and `result` = "You've reached your Fable limit. Switch to another model, or manage usage credits at …". The runner judged the exit code first and the note said `model returned nothing (rc 1; non-zero exit)`, throwing away the only useful sentence in the run. The JSON is now read before the exit code; a refusal is its own outcome and the note is `model call refused [fable]: <the model's own sentence> — try --model sonnet (no model is substituted automatically)`.
- **`PROVLEDGER_ASK_MODEL`** names the model (`--model` wins), resolved once in `ask.run` so both model calls, the note and `ask_log.model` agree. Nothing retries with a different model: an answer whose model was swapped in silently is an answer whose provenance is a guess.
- **Headless calls stop inheriting the host's environment.** `claude -p` reads the user's `~/.claude/settings.json`; on the machine this was found on it says `"language": "Chinese"`, so every headless call (ask, `ClaudeArbiter`, `significance`, the external trigger) answered in Chinese against English prompts — and asking for English *in the prompt* does not override it. `claude_command()` now passes `--settings` pointing at a file of ours (`{"language": "en"}`, one temp file per process, removed at exit; `$PROVLEDGER_CLAUDE_SETTINGS` overrides).
- **The summary is a JSON object, so plugin noise falls off.** The host's plugins write into `result`: claude-mem prepends "Memory capture is currently paused due to a quota cooldown…" to every answer, and `enabledPlugins: {}` in our settings does not stop it. Parsed as prose it counted as *a sentence the model invented with no citation*. `prompts/ask.md` now demands `{"sentences": [...]}` and `summarize.parse_sentences` takes that object out of the reply; a reply that is not that shape is its own degraded reason (`not_json`) with the raw head kept.
- **The answer is in the language you asked in, and that is reported, never enforced by deletion.** `--lang` / `?lang=` switched the scope line only, so a model answering in Chinese against an English prompt went through unremarked; `prompts/ask.md` now asks for English right after the answer shape, with an English example. Deleting a CJK sentence from an `en` answer produced `(nothing survived the checks)` over a paragraph whose every citation was right. Citations and numbers are correctness and still delete; language is a preference, so `dropped["language"]` is gone and `language_mismatch` (`None` | `some` | `all`) travels with the answer, with a note naming the fix.
- **CLI**: `--runner` defaults to `$PROVLEDGER_ASK_RUNNER` (the dashboard has read it since phase 2e and the CLI ignored it), accepts `none`, and `--timeout S` (default 180) bounds each model call.

### Implemented, and not switched on
- **A judge for changes to decks and reports**: when a change lands on a figure
  or a conclusion rather than on code, a model decides — from five worked pairs,
  and under the rule that it stays quiet when unsure — whether the change is one
  nobody explained, and whether the reason is already in what you said. Every
  verdict is logged so the false-question and missed-change rates can be counted.
  It must clear the same numeric gate as the identity arbiter before it may ask
  anyone anything; on this release's evaluation it did not (consistency 0.60,
  accuracy 0.60), so it stays off. A refusal is a result, not a failure.
- The identity arbiter (0.2.0) is still behind its own gate for the same reason.

### Design and packaging
- A shared token file feeds both the dashboard and a React component library;
  the interface reads in plain professional English, with Chinese under
  `?lang=zh`; switching views glides instead of reloading.
- A reproducible demo (`examples/phantom-uplift/demo-provenance.sh`) builds the
  whole scenario in seconds, and a walkthrough recording is generated from it.
- `provledger` on PyPI now ships the console script the documentation uses.
- The repository reads in English throughout: the interface wording, the CLI
  output and the documents. The Chinese vocabulary file stays, because it is
  what `?lang=zh` is made of. Four Chinese strings that were reaching the
  English pages regardless of the setting now go through it instead.
- The deferred-work ledger is split: [`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md)
  publishes the confirmed defects and standing limits with their workarounds,
  and the working log stays out of the repository.
- README section 2 shows the map as an animation and the dashboard as the three
  views a person actually walks: a task turning red on a silent upstream change,
  the node history behind it, and the graph the warning is computed from.

## 0.2.0 — 2026-09-15

The dual-layer node model, end to end: a project's code is observed as
nodes with stable identities across runs, every plan closes with reasons and
expectations, outcomes are measured later by channels, and an arbiter may
link identities only after clearing a numeric gate. 1048 tests across seven suites.

### Phase 8 — calibration by construction, a real arbiter through the gate, two dashboard pages
- **Calibration set generated by construction** (FL-041): corpus swap variants declare their truth in `expect.toml`, negatives (2:2 "none"), partial links (1:2) and dataflow-layer variants are built programmatically; `analyzer calibration generate|stats`; 48 items (pairs 16 / none 16 / partial 16) plus the person-labelled live export.
- **`ClaudeArbiter`** (FL-040): a headless-`claude` arbiter (`claude -p --output-format json --tools ""`, the user's own login, no API key) with a strict JSON answer contract; offline tests inject a runner; it runs by hand through `arbiter-eval` and is wired only if the gate passes. Run once on the 67-item set (×3): consistency 0.701 / coverage 0.433 / accuracy 0.552 — **refused** (consistency < 1.0), not wired; it never produced a wrong pair.
- **`/outcomes`** (FL-042): every claim across plans with its latest outcome, tier labels, delta / signal, who stated and who backfilled it; `?project=` filter.
- **`/node/{project}/{qualified_name}`** (FL-009): a node's upstream / downstream (card), its history grouped by run (with plan links and tier labels, `identity_asserted` shown with its evidence), its close-time reasons and anchored constraints (a restricted constraint shows only `why_ref`) — one read-only query; the plan page's Reasons panel links to it.
- `Row.context`, `psg_bridge.card_of / latest_qualified_name`, `docs/arbitration.md` §2b/§6, `CHANGELOG.md`.

### Phase 7 — outcome channels, arbitration interface, history backfill (PR #43, 2026-09-14)
- **Outcome channels** (FL-007): `metrics` table + `record-metric` / `metrics_from_stdout`, `OutcomeChannel` protocol, `ProfileDriftChannel`, `MetricChannel` (nearest-before / after, delta_pct), third-party channels via `provledger-extensions.json`; the phantom-uplift demo's +23% becomes an observed outcome of a "±5%" claim; 🎯 Outcomes panel on the plan page.
- **Arbitration interface + gate** (FL-027 part): `graph_api.Arbiter`, `HeuristicArbiter`, `provledger.testing.calibration` (run / report / gate: consistency 1.0, accuracy ≥ 0.9 on ≥ 10 labelled, sha match), `analyzer ambiguities --export`, `analyzer arbiter-eval`, gated wiring in `cli.run`, `selfcheck arbiter_gate`.
- **History backfill** (FL-004): `analyzer backfill <repo> --since [--until] [--every] [--subdir] [--fresh-db]` replays past commits into a graph through worktrees, `trigger=backfill`, resumable.
- FL-034 handled at the review gate (`re_exported_symbols` → warning); `run_provider(isolate="subprocess")`; `docs/outcomes.md`, `docs/arbitration.md`.

### Phase 6 — the host API in the package, conformance, built-ins as reference providers (PR #42, 2026-09-14)
- `provledger.graph_api` (Signature / NodeObservation / ExtractionContext / NodeTypeProvider / the host matcher `match`) — the analyzer's only backend import is `analyzer._host` (FL-006).
- `provledger.testing.conformance.run`: six contracts every node-type provider must keep (determinism, purity, stability matches declaration, schema, failure isolation, performance budget); a liar provider is caught; `docs/conformance.md`.
- `provledger.providers` (run_provider with isolation and degradation records, `builtin_symbols` / `builtin_owned` as reference providers), `extensions_json.providers`, `selfcheck providers_degraded`.
- FL-029 (bare-name gates → qualified resolution), FL-030 (`review_run --as-recovery`, `--accept-stale`, reopened close checks the registry sha).

### Phase 5 — declarative registration (PR #41, 2026-09-14)
- `provledger-extensions.json`: constraints, analyzer name sets, drift kinds, providers and outcome channels registered without source changes; discovery, priorities, fingerprint on every run (`analysis_run.extensions_json`); `ledger_cli import`; `docs/extensions.md`.
- FL-028 (aborted runs marked, never a predecessor), survival re-judged per close, `review_run` dirty-tree guard.

### Phase 4 — scenario suite and the deterministic review driver (PR #40, 2026-09-14)
- `skills/project-state-graph/scripts/tests/scenarios/`: a synthetic project, nine timeline scenarios asserted as event streams with `must_not`, one golden per scenario, fully isolated.
- `review_run.py`: the update-project-state-graph review as one deterministic, injectable driver (gates → refresh → reason slots → fill → close); FL-023 / FL-024 fixes; migration self-heal; `test_llm_consistency.py` (deselected by default).

### Phase 3.5 — explicit project attribution (PR #39, 2026-09-13)
- `Plans.project` / `project_source` decided at publish (declared, `"none"`, or the repo published from) — FL-014; migrations run on every open (FL-021); re-open after a regular-step recovery (FL-022); slimmer `impact_context`.

### Phase 3 — reasons, constraints, tiers (PR #38, 2026-09-11)
- Close-time **reason slots** (a closed set: the nodes the plan changed), `unstated` backstopped by the system, never invented; **anchored constraints** with `why_visibility` (restricted rationale never leaves the ledger); tier labels (`observed` / `derived` / `asserted` / `stated` / `unstated`) on the dashboard, text first, colour second.

### Phase 2 — node history (PR #37, 2026-09-10)
- `node_snapshot` / `node_event` (append-only), three-layer identity (qualified name, structural signature, dataflow signature) plus owner inheritance; `identity_ambiguous` is recorded, never silently linked; `analyzer history <node>`.

### Phase 1 — the refactor-mutation corpus (PR #35, PR #36, 2026-09-10)
- `provledger.testing` corpus (cases × mutations) and the identity-stability harness; phase-1 dogfood fixes FL-011 / 012 / 013 / 015; `python3` everywhere.

## 0.1.0 — 2026-07-24
- First pip-installable `provledger`: the orchestrator backend (plans, steps, deviations, circuit breakers, migrations), the four skills, the read-only dashboard, the project-state-graph analyzer, the phantom-uplift and silent-class-drop demos.
