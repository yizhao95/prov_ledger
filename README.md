# 🧾 provLedger

**prov**enance **+** **Ledger** — a memory system that makes a data scientist's coding
agent reason with engineering discipline: it holds a complete, auditable chain from
**plan → verified change**, and treats **upstream data as a first-class contract**
rather than an assumption.

> *"My coding agent should, in every situation, hold a complete reasoning chain —
> and never repeat a past mistake."*

---

## ★ The Goal

A data scientist's agent that thinks less like a careless coder and more like a
disciplined engineer — **without losing the data-science instincts.**

- **Half 1 — Complete reasoning chain in scope.** Before writing code, the agent
  should know which modules / functions / variables / DataFrame columns a change
  touches; how the main pipeline is affected; what is added, removed, or modified;
  and how upstream & downstream must change in step.
- **Half 2 — Never repeat a mistake (the ledger).** A provenance ledger of past
  decisions and failures, searched by fuzzy match at plan time. Its first real job
  is data-engineering decision memory — e.g. *"this is a time-series split, so we
  use a rolling window, not a random split — random split leaks temporal
  information."*

---

## 1 · Two Pillars, One Database Discipline

Two independently useful subsystems that share **one philosophy** and **one SQLite store**.

| | Pillar | Records |
|---|---|---|
| **A** | **Orchestrator** — plan / step state machine | WHAT WAS DONE |
| **B** | **Project State Graph** — builder + reviewer | WHAT THE CODE & DATA ARE |
| **→** | **Result** | Auditable chain + contract safety net — reasoning captured, breakage blocked |

**Pillar A — Orchestrator.** A SQLite-backed plan/step state machine driven by two
iron-law skills (`writing-plans`, `executing-plans`). The LLM composes a plan and
calls thin shell wrappers; a Python + SQL backbone validates every transition,
enforces circuit breakers, and captures logs.

**Pillar B — Project State Graph.** A two-layer map of a repo: a deep SQLite
node/edge graph built by analyzers, plus a human-readable `ARCHITECTURE.md`. A
companion reviewer (`update-project-state-graph`) checks each change against the
graph at close time — code contracts **and** data contracts.

🔗 **How they connect:** the graph is read at two moments — once at **planning time**
(predict impact, including upstream-data assumptions) and once at **review time**
(verify nothing broke). Same graph, same precomputed cards, two timestamps.

---

## 2 · Core Philosophy — Deterministic Backbone × LLM Decision Layer

LLM agents drift: they forget to log output, forget to mark steps done, mis-guess
which downstream a change affects, and — for a data scientist especially — silently
corrupt a schema or a split. The fix is to factor out **everything deterministic**
into a Python + SQL backbone and leave only genuine decisions to the LLM.

| Layer | Owns |
|---|---|
| **LLM decides** | What to do next · which symbols / columns a feature touches · how to write the code · whether a deviation is needed |
| **Backbone records / enforces** | State transitions · circuit breakers · log capture · dependency edges from AST · code & data contract gates · plan closure |

**The rule:** *every invariant the backbone enforces is one the LLM can never
accidentally violate.* The LLM may be creative; the backbone may not.

🔬 **Why this is the data-science-shaped version:** a software engineer's agent must
not break the call graph. A data scientist's agent must not break the call graph
**and** must not silently corrupt a column schema, a train/test split, or an
upstream-table assumption.

---

## 3 · Task Lifecycle

A state-altering task that touches a registered project, end to end:

| # | Phase | Owner | What happens |
|---|---|---|---|
| 1 | Intent classify | Orchestrator | `STATE_ALTERING` or `STATELESS`? Email/docs skip the heavy path. |
| 2 | Pre-flight + plan | `writing-plans` | Pull impact for touched symbols/columns + upstream-data assumptions; fuzzy-match the ledger for relevant past decisions; publish Plan + Steps + REVIEW step. |
| 3 | Execute | `executing-plans` | Each step runs through a thin wrapper; `run-step.sh` atomically captures stdout/stderr + true exit code. |
| 4 | Park | Orchestrator | All steps terminal → plan parks in `NEEDS_REVIEW` when a registered project was touched. |
| 5 | Review | `update-project-state-graph` | Diff vs the graph → code & data contract gates. Clean → refresh + re-test + close. Broken → FAIL, report, human decides. |

---

## 🧬 Origin & Attribution

The two orchestration skills — **`writing-plans`** and **`executing-plans`** — are
**evolved from the [Superpowers](https://github.com/obra/superpowers) skill library**
by Jesse Vincent (obra). The original Superpowers skills established the
plan-then-execute discipline and the "thin shell wrapper + deterministic backbone"
philosophy. This repository builds on that foundation.

### What this project adds on top of the original Superpowers skills

- **A SQLite-backed orchestrator** (`orchestrator-backend/`) — the plan/step state
  machine is no longer ad-hoc markdown; every transition is validated and persisted
  in a real database with migrations, circuit breakers, and immutable `COMPLETED`
  steps.
- **Mandatory log capture** — `run-step.sh` atomically records stdout/stderr and the
  *true* exit code (hardened against `PIPESTATUS` masking).
- **A second pillar — the Project State Graph** (`project-state-graph` +
  `update-project-state-graph`) — a deep node/edge code graph plus a reviewer that
  gates every change against **code contracts and data contracts**.
- **Data-as-first-class-citizen modeling** — a function's output is a typed
  `data_var` with `produces` / `consumes` edges, turning *"changed output type → who
  breaks?"* into a single lookup.
- **DataFrame-aware contracts** — a DataFrame-producing function's "signature" is its
  column-set + per-column dtypes; dropped / renamed / retyped columns are caught.
- **Plan-time impact pre-flight** (`impact_preflight.py`) — forward impact analysis
  (declared targets, upstream assumptions, capability boundary) attached to each plan.
- **A decision-memory ledger** (`ledger_store` / `ledger_cli`) — past decisions and
  anti-patterns surfaced by fuzzy match at plan time as advisory reminders (never a
  hard gate).
- **A live read-only dashboard** (`orchestrator-webapp/`) — a FastAPI + HTMX
  dashboard that reads the same SQLite DB read-only; the orchestrator works correctly
  even if the dashboard is down.
- **`dtype` / schema coverage as a visible metric** — every data contract gate is
  exactly as strong as dtype coverage, so the unknown share is surfaced as a number.

---

## 4 · Strengths

- **Battle-tested invariants** — circuit breakers, immutable `COMPLETED` steps, and
  mandatory log capture, hardened against real incidents.
- **Data as first-class citizens** — typed `data_var` nodes with `produces`/`consumes`
  edges.
- **Cards, not traversals** — per-callable `consistency_card`s precomputed purely from
  edges, so the LLM does one retrieval instead of a graph walk.
- **Report, don't auto-fix** — the reviewer detects breakage deterministically but
  never repairs it; a human decides.
- **Generic property-graph schema** — new node/edge vocabularies (SQL, API, ML, DE)
  are added with no migration.
- **Read-only observability** — the dashboard reads the same DB read-only; the
  orchestrator never depends on it.

---

## 📦 Repository Layout

```
prov_ledger/
├── orchestrator-backend/      # Pillar A — SQLite plan/step state machine (stdlib only)
│   ├── orchestrator/          # api.py, db.py, state_machine.py, circuit_breakers.py
│   ├── migrations/            # 001..009 SQL migrations
│   └── tests/                 # 111 tests
├── orchestrator-webapp/       # Live read-only provLedger Dashboard (FastAPI + HTMX)
│   └── app/                   # main.py, queries.py, templates/
├── orchestrator-cli.py        # CLI entry point
└── skills/
    ├── writing-plans/             # ← evolved from Superpowers
    ├── executing-plans/           # ← evolved from Superpowers
    ├── project-state-graph/       # Pillar B — deep code/data graph builder
    ├── update-project-state-graph/# Pillar B — reviewer / contract gates
    ├── brainstorming/             # supporting process skills
    ├── systematic-debugging/
    ├── verification-before-completion/
    ├── test-driven-development/
    └── subagent-driven-development/
```

---

## 🚀 Installation

### Recommended — install as a Claude Code plugin (one command)

```text
/plugin marketplace add yizhao95/prov_ledger
/plugin install provledger@provledger
```

Dependencies install themselves on first session: a background `SessionStart`
bootstrap builds **one** venv at `~/skill-workspace/.venv` (override with
`PROVLEDGER_VENV`) and installs `requirements.txt`. It is idempotent — warm
sessions are a no-op. Launch the review dashboard any time with
**`/provledger-dashboard`**.

> **Companion (recommended):** install the
> [`superpowers`](https://github.com/obra/superpowers) plugin for the full set of
> supporting process skills. provLedger bundles only its evolved and novel skills
> (`writing-plans`, `executing-plans`, `project-state-graph`,
> `update-project-state-graph`, plus locally-adapted `brainstorming`,
> `systematic-debugging`, `test-driven-development`,
> `subagent-driven-development`) and treats superpowers as a **soft dependency**:
> the byte-identical `verification-before-completion` skill is not bundled and is
> provided by superpowers when present.

### Manual / development install

See **[INSTALL.md](INSTALL.md)** for the full guide. Quick start:

```bash
# 1. Clone
git clone git@github.com:yizhao95/prov_ledger.git
cd prov_ledger

# 2. Bootstrap dependencies into the unified venv (idempotent)
bash scripts/bootstrap.sh
PY=~/skill-workspace/.venv/bin/python

# 3. Run the test suites to confirm a healthy install (run each separately —
#    each suite has its own pyproject/pythonpath; one combined invocation breaks)
$PY -m pytest orchestrator-backend -q
$PY -m pytest orchestrator-webapp  -q
$PY -m pytest skills/writing-plans/tests -q
$PY -m pytest skills/executing-plans -q
$PY -m pytest skills/project-state-graph/scripts/tests -q
$PY -m pytest skills/update-project-state-graph/scripts/tests -q

# 4. Launch the dashboard
PROVLEDGER_WEBAPP_DIR=orchestrator-webapp bash orchestrator-webapp/launch_dashboard.sh
# → open http://127.0.0.1:8765
```

The dashboard reads the orchestrator database **read-only**. Point it at any
orchestrator DB with the `ORCH_DB` environment variable (defaults to
`~/skill-workspace/orchestrator.db`).

---

## 📫 Contact

Maintainer: **yzhao950213@gmail.com**

## 📄 License

MIT — see [LICENSE](LICENSE).
