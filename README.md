# provLedger

Lost in a project's decision history? Watching your coding agent walk the same wrong path again?

Someone asks in chat why the churn model splits 70/30 — *wasn't it the other way round?* You scroll back through four months of threads, find three conversations that nearly say it, and give up.

```
$ /ledger why is our train/test split 70/30?

  70/30 has been in force since 2026-04-03. Moving to 80/20 was tried on
  2026-08-14 and rejected: the holdout leaked week-52 promotions, so the
  lift was the promotion and not the model.          [#94 · your own words]

  The email that settled it is on the record.
  Sarah Chen, "Q3 rollup scope", 2026-08-14 09:12    [r12 ↗ · checked 6d ago]

  Raised again on 2026-09-17 and dropped without an answer.  [#i9]

  Searched 3 nodes · 2026-04-03 to 2026-09-17 · nothing left out
```

Every line ends in a record id. The ids are real rows, the search range is counted rather than estimated, and when nothing was recorded the answer says so instead of filling the gap.

**Your project, answering for itself** — across every node, every task and every month it has existed.

![A task page, the decisions it relied on, the rule behind one of them with the email that carried it, and /ledger answering a question with the record it rests on](docs/media/readme-hook.gif)

*Task → "Decisions relied on" → open the node → the rule with the email → `/ledger`: "why did we stop using orders.discount?" → a cited answer.*

**[Watch it happen →](https://yizhao95.github.io/prov_ledger/walkthrough.html)** — two minutes: a colleague asks why the data stops on 18 September, nobody on the team knows, and the record answers with the incident behind it.

A Claude Code plugin for data work. It keeps what was decided and why — your words, the email, the agent's reading, kept apart and never blended — and puts them in front of whoever is about to change the thing again.

[![PyPI](https://img.shields.io/pypi/v/provledger)](https://pypi.org/project/provledger/)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

---

## 1 · The problem

Three months ago someone said, in a meeting, "don't split that dataset randomly — it is a time series." Today a new teammate, or a coding agent, reads the odd-looking rolling-window split, finds no comment and no failing test, and cleans it up. Every test passes. The number on the quarterly deck quietly changes. Nobody knows why.

`git blame` gives you who and when. Lineage gives you where the column came from. An architecture decision record gives you the shape of the system. None of them connects the line you are looking at to the email, the meeting or the sentence that put it there — because the reason was never inside the code or the data in the first place.

---

## 2 · What it does

It keeps one view of a project across nodes (the whole map and what flows through it), across time (what each thing was, and what it became) and across tasks (what every piece of work read, decided and expected). Graph, Node and Task are three angles on that one view, not three pages: they share a single anchor of project, node and point in time, and switching between them keeps it — one anchor, three angles. Around each thing in the map it holds the decisions: why it changed, the rule that still constrains it, and **the paths that were tried and dropped** — the half a diff never shows.

![A data-flow picture of a real repository, switching between two views: 3,568 nodes folded into modules and laid out in three bands — what is read, what processes it, what is produced — with the count of nodes that carry records changing as the view changes](docs/media/readme-graph.gif)

*Data flow, not a call graph: 3,568 nodes folded into 11 modules, laid out as what gets read, what processes it, and what comes out.*

A dataset column, a SQL table, an external feed, a metric, and anything you declare in a sentence ("EMEA excluded from the Q3 rollup, decided in the March review") are all first-class, each with its own page, history and rules.

The knowledge comes from three sources, kept apart:

- **Measured** — the map is rebuilt from the source on every run and compared with the run before, so what changed is counted rather than reported.
- **Said** — your words, recorded verbatim as you type them.
- **Evidenced** — an email, a meeting note, a ticket: a label, a time and a link, never the body.

When none of them has anything to say, the record says *unstated* and stops there.

| the question | what answers it |
|---|---|
| Why is it this way? | `/ledger`, and every sentence ends in the id of the record it rests on |
| Have we tried this before? | the rejected paths on the thing you are about to change, surfaced before you edit it |
| What would this change reach? | the computed arrows — consumers, and the metric three steps downstream |
| Someone is asking me to justify this | `/receipts` — the same records as a timeline with their sources, for your own model to write the reply from |
| I just inherited this project | all of the above, without having been in any of the meetings |

![A task page with its findings block in red: two blocking findings, one of them an upstream column that stopped arriving, and one finding still unanswered](docs/media/readme-task.png)

*Task — the check runs before the edit, not after it.*

![A node page: one thing's change history, with the record this change touched marked in place and its hit count raised by one, and the constraints panel highlighted in step](docs/media/readme-node.png)

*Node — one thing's timeline, and which of its past decisions this change touched.*

![A data-flow graph of the whole project, modules laid out in three bands with the arrows between them](docs/media/readme-state-graph.png)

*Graph — the same project as a map, so a change can be read against what is upstream and downstream of it.*

![The /ledger command answering a question in the terminal, every sentence ending in the id of the record it rests on](docs/media/readme-ask.gif)

*Every sentence cites a record. When nothing is recorded, it says so.*

---

## 3 · Five minutes

### Install

As a Claude Code plugin — this brings the hooks, the skills and the dashboard:

```bash
claude plugin marketplace add yizhao95/prov_ledger
claude plugin install provledger@provledger
```

Or only the core library, to read and write a ledger from your own code (no hooks, skills or dashboard; see [`INSTALL.md` §5b](INSTALL.md)):

```bash
pip install provledger
```

Dependencies install themselves on the first session. The manual install and troubleshooting are in [`INSTALL.md`](INSTALL.md).

### Five commands

Each command below was run for real against a scratch ledger; the lines under it are what it printed (`…` marks lines left out).

**1 · Confirm the plugin is in.**

```console
$ claude plugin list
  ❯ provledger@provledger
    Version: 0.4.7
    Scope: user
    Status: ✔ enabled
```

**2 · Register a project.** This reads a git repository into the map and writes the first run. `--repo` must already exist; here it is a scratch repository with one commit.

```console
$ bash skills/project-state-graph/scripts/init_project.sh \
    --name myproj --repo /tmp/myproj --out-dir /tmp/myproj-graph
…
    registered myproj -> /tmp/myproj-graph/myproj-state-graph.db
==> [4/5] verifying built graph (selfcheck)
Self-check: PASS (/tmp/myproj-graph/myproj-state-graph.db)
…
```

**3 · Run one plan.** The bundled demo is the whole arc in miniature, on its own scratch project under `/tmp/provledger-demo`: a person removes a column and records why, with the email that carried it; then an agent plans to use that column again.

```console
$ bash examples/phantom-uplift/demo-provenance.sh
…
     2 findings unanswered · 2 records shown · 0 adopted
…
  ORCH_DB=/tmp/provledger-demo/orchestrator.db
  PSG_REGISTRY_PATH=/tmp/provledger-demo/projects.json
```

Both findings are the heads-up the second plan was given before it wrote a line. The last two lines are the scratch ledger and graph registry the next two commands read.

**4 · Open the dashboard** over that ledger (inside a session, `/provledger-dashboard` does the same for your own).

```console
$ ORCH_DB=/tmp/provledger-demo/orchestrator.db PSG_REGISTRY_PATH=/tmp/provledger-demo/projects.json \
    PROVLEDGER_DASH_PORT=8766 bash orchestrator-webapp/launch_dashboard.sh
…
✅ Dashboard ready at http://127.0.0.1:8766
```

**5 · Ask one question.** `provledger` is installed in the plugin's venv, `~/skill-workspace/.venv/bin/`.

```console
$ ORCH_DB=/tmp/provledger-demo/orchestrator.db PSG_REGISTRY_PATH=/tmp/provledger-demo/projects.json \
    provledger ask "why did we stop using orders.discount?" --no-model --project phantom-uplift-demo
Q: why did we stop using orders.discount?

summary unavailable: no model configured

Absences
  `pkg.rollup.load_orders` has never been verified: no outcome is recorded for it in scope. [scope]
…
Scope: 3 nodes, 2 constraints, 0 influencing records, 6 changes, 2026-10-04 to 2026-10-04; 3 candidates, 3 chosen; nothing truncated. Computed in 0.1 s.
…
```

![The same question asked and answered in the terminal, with the scope line and the records it cites](docs/media/readme-cli.gif)

*The whole answer, in the terminal you are already in — the facts, the absences, and the range that was searched.*

---

## 4 · What it records on your machine

Everything stays local, in SQLite files under `~/skill-workspace/`. The hooks only ever add:

| hook | what it records |
|---|---|
| `SessionStart` | installs dependencies once, in the background |
| `UserPromptSubmit` | your prompt, verbatim, attributed to the repo and the open plan |
| `PostToolUse` / `PostToolUseFailure` | one row per tool call, failed ones marked, for the cost numbers |
| `PreToolUse` (Edit / Write / MultiEdit) | shows the rules anchored on the lines about to change — at most 600 characters, nothing when nothing anchors there |
| `Stop` | closes the session record and refreshes the graph |

None of them blocks, executes or decides, unless you opt in by declaring a constraint with `"block": true`. The dashboard opens the ledger read-only; the one thing it writes is a log row for each question asked on its `/ledger` page. How records are labelled, chained and anchored in git is in [`docs/decision-provenance.md`](docs/decision-provenance.md).

---

## 5 · More

| | |
|---|---|
| [`INSTALL.md`](INSTALL.md) | plugin and manual install, verification, PyPI, the dashboard, troubleshooting |
| [`docs/cli.md`](docs/cli.md) | every `provledger` command, and the usual way in: from a line of your own code |
| [`docs/NORTH-STAR.md`](docs/NORTH-STAR.md) | the one sentence and the three cores every change is measured against |
| [`docs/decision-provenance.md`](docs/decision-provenance.md) | the tables, the tiers, the rules, the headline, `/ledger`, verify and export, anchors, cost, and the honest boundaries |
| [`docs/conformance.md`](docs/conformance.md) · [`docs/extensions.md`](docs/extensions.md) · [`docs/outcomes.md`](docs/outcomes.md) · [`docs/arbitration.md`](docs/arbitration.md) | writing your own node types, registering everything, outcome channels, identity arbitration |
| [`orchestrator-webapp/`](orchestrator-webapp/README.md) · [`docs/design.md`](docs/design.md) | the dashboard's pages; its design system |
| [`docs/benchmark-silent-class-drop.md`](docs/benchmark-silent-class-drop.md) | the failure class in numbers: a silently dropped column, segment purity 0.31 against 0.91 on the same green pipeline |
| [`examples/phantom-uplift/`](examples/phantom-uplift/) | `make demo` — a revenue number that goes up for the wrong reason, caught offline and deterministically |
| [`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md) | confirmed defects and standing limits, with workarounds |
| [`CHANGELOG.md`](CHANGELOG.md) | what each release added |
| [`LICENSE`](LICENSE) | MIT |

### Origin

The two orchestration skills, `writing-plans` and `executing-plans`, are evolved from the [Superpowers](https://github.com/obra/superpowers) skill library by Jesse Vincent (obra), which established the plan-then-execute discipline this repository builds on. What provLedger adds is the persistence and the provenance: a validated SQLite state machine instead of ad-hoc markdown, a project state graph with code and data contract gates, the decision ledger, and the read-only dashboard.

### Contributing

Issues and pull requests are welcome. Good first contributions: run `make demo` and report anything that does not reproduce; add a second silent-failure scenario to the demo; improve dtype coverage of an analyzer in `skills/project-state-graph/`. The tests run with `bash scripts/run_tests.sh`; conventions are in [`CLAUDE.md`](CLAUDE.md). Maintainer: yzhao950213@gmail.com.

### See it run

<video src="https://yizhao95.github.io/prov_ledger/media/walkthrough.mp4" controls muted playsinline width="880" poster="https://yizhao95.github.io/prov_ledger/media/walkthrough-cover.png"></video>

*Two minutes, no narration. Someone asks in chat why the rollup stops on 18 September and nobody still on the team knows. The record does: an upstream incident, the RCA that found it, the call that decided to cut the data — and that the cutoff was only ever meant to be temporary.* Chapters and captions in English or Chinese: **[the walkthrough page](https://yizhao95.github.io/prov_ledger/walkthrough.html)**.
