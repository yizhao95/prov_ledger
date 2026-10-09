# Installation Guide

This guide walks you through installing **provLedger** from scratch, verifying the
install, and launching the dashboard.

---

## 0 · Fastest path — install as a Claude Code plugin

Inside a Claude Code session:

```text
/plugin marketplace add yizhao95/prov_ledger
/plugin install provledger@provledger
```

Or from a shell:

```bash
claude plugin marketplace add yizhao95/prov_ledger
claude plugin install provledger@provledger
claude plugin list          # provledger@provledger · Version: 0.4.6 · Status: ✔ enabled
```

Dependencies install themselves on first session: a background `SessionStart`
bootstrap builds **one** venv at `~/skill-workspace/.venv` (override with
`PROVLEDGER_VENV`) and installs `requirements.txt`. It is idempotent — warm
sessions are a no-op — and re-installs when `requirements.txt` or
`orchestrator-backend/pyproject.toml` changes, so a plugin update reaches the venv. Launch the dashboard with `/provledger-dashboard`, or
`bash orchestrator-webapp/launch_dashboard.sh` (port 8765 by default,
`PROVLEDGER_DASH_PORT` to change it, `PROVLEDGER_WEBAPP_DIR` to point at a
checkout other than the script's own).

**Companion (recommended):** install the
[`superpowers`](https://github.com/obra/superpowers) plugin. provLedger treats it
as a **soft dependency** — the byte-identical `verification-before-completion`
skill is not bundled and comes from superpowers when present; the other shared
skills are bundled with local adaptations. Everything still works without
superpowers, just with fewer supporting process skills.

**Same-named skills:** provLedger bundles local variants of six superpowers
skills (`brainstorming`, `writing-plans`, `executing-plans`,
`subagent-driven-development`, `test-driven-development`,
`systematic-debugging`). With both plugins enabled, `bootstrap.sh` prints a
notice on every SessionStart. To let provLedger's variants win in one project:

```bash
claude plugin disable superpowers@claude-plugins-official --scope local
```

`--scope local` writes the override to `.claude/settings.local.json` in that
project only — your user-level superpowers install stays enabled elsewhere.

The rest of this guide is the **manual / development** install.

---

## 1 · Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.13** | The Project State Graph analyzers use `tree-sitter` wheels built for 3.11+; 3.13 is recommended and is what the test suite is validated against. The orchestrator backend itself is **stdlib-only** and runs on 3.10+. |
| **git** | To clone the repository. |
| **A C toolchain** | Only needed if `tree-sitter` wheels must build from source on your platform (most platforms ship prebuilt wheels). |
| *(optional)* **[uv](https://github.com/astral-sh/uv)** | A fast drop-in replacement for `venv` + `pip`. Examples below show both `pip` and `uv`. |

---

## 2 · Clone the repository

```bash
git clone https://github.com/yizhao95/prov_ledger.git
# (ssh works too if you have a key on this account:
#  git clone git@github.com:yizhao95/prov_ledger.git)
cd prov_ledger
```

---

## 3 · Create a virtual environment

### Option A — standard library `venv`

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

On Debian and Ubuntu — which includes WSL — the system Python does not ship
`ensurepip`, and that first line fails with *"The virtual environment was not
created successfully because ensurepip is not available."* Install the package
it names and run it again:

```bash
sudo apt install python3-venv     # or python3.12-venv, matching your version
```

Option B needs no such thing, which is a good reason to prefer it if you have
no `sudo` on the machine.

### Option B — `uv` (faster)

```bash
uv venv --python 3.13 .venv
source .venv/bin/activate
```

---

## 4 · Install dependencies

provLedger has a deliberately small dependency surface:

| Component | Dependencies |
|---|---|
| `orchestrator-backend` | **none** (Python standard library only) |
| `orchestrator-webapp` (dashboard) | `fastapi`, `uvicorn[standard]`, `jinja2` |
| `project-state-graph` analyzers | `tree-sitter` + language grammars |
| Test suites | `pytest`, `httpx` (the dashboard tests' client) |

Install everything, from the repository root (the `-e ./orchestrator-backend` line
is resolved against the current directory):

```bash
# if you took Option A (`python3 -m venv`) — that venv has pip
pip install -r requirements.txt

# if you took Option B (`uv venv`) — that venv has NO pip, so use uv
uv pip install -r requirements.txt
```

`requirements.txt` is the one authoritative list, and its last line installs the
backend itself (`-e ./orchestrator-backend`), which is what makes `import
provledger` and the `provledger` command work. The table above says what is in it
and why; the file says it in a form you can run.

This used to be a dependency list typed out here instead. It drifted, exactly the
way a second copy does: it omitted `httpx` — which `starlette.testclient` needs to
drive the dashboard in §5 — and it never installed the backend at all, so the
suites failed on `No module named 'provledger'` and the dashboard's graph and
`/ledger` pages rendered "unavailable". None of that was visible on a machine that
already had those things.

> **The two blocks are not interchangeable.** `uv venv` deliberately creates an
> environment without `pip`, so `pip install …` there fails with
> `No module named pip` — a confusing error, because nothing above says the
> choice in §3 decides the command in §4. Match the block to the option you took.

### 4a · Check that `provledger` is on your PATH

```bash
provledger --help          # must print the command list
```

`requirements.txt` installed it, so this should already work. If it does not, stop
here rather than carrying on: the command comes with the `provledger` package, which
the dashboard's graph and `/ledger` pages import. Without it they serve pages that
say "unavailable" instead of failing in a way you can act on — which reads as a
broken dashboard.

> **Minimal install (orchestrator only, no graph, no dashboard):**
> the backend needs nothing beyond Python + `pytest` for the tests.

### 4b · The state-graph analyzer's `uv` environment

`skills/project-state-graph/scripts/` is a `uv` project of its own:
`init_project.sh` (and therefore every review refresh) runs the analyzer with
`uv run python -m analyzer`, inside the environment described by that
directory's `pyproject.toml` — **not** inside the venv above. Since phase 6 it
depends on the `provledger` package as an **editable path source**
(`[tool.uv.sources] provledger = { path = "../../../orchestrator-backend", editable = true }`),
so third-party `NodeTypeProvider`s registered in `provledger-extensions.json`
can `import provledger.graph_api` when the analyzer runs them.

```bash
cd skills/project-state-graph/scripts
uv sync                       # resolves + installs into scripts/.venv (first run: ~1 min)
uv run python -c "import provledger.graph_api, analyzer; print('ok')"
```

`uv.lock` is **not committed** (git-ignored): every clone resolves it locally.
Re-run `uv sync` after pulling a change that bumps `orchestrator-backend`'s
version or its dependencies; an out-of-date environment shows up as
`WARNING: provider <id> degraded: ModuleNotFoundError: provledger...` on the
analyzer's stderr and as a `providers_degraded` warning in `selfcheck.py`.

Other analyzer subcommands: `history`, `backfill` (replay past commits into a
fresh graph), `ambiguities` / `arbiter-eval` (see `docs/arbitration.md`).

The analyzer also accepts `--isolate subprocess` (env `PROVLEDGER_ISOLATE=subprocess`
for `init_project.sh`): each provider's `extract()` is then forked and **killed**
on timeout instead of abandoned in a thread (the default `--isolate thread`).

---

## 5 · Verify the install

Run the test suites. Each runs in its own pytest process — a combined
invocation breaks (see [`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md)) — and the
script does that for you. A healthy install passes all of them:

```bash
bash scripts/run_tests.sh              # every suite, one at a time
bash scripts/run_tests.sh --list       # the suite names
bash scripts/run_tests.sh backend psg  # only some of them
bash scripts/run_tests.sh --count      # collect only: how many tests there are
```

The script uses the active virtualenv (the one you made in §3), or the plugin's
unified venv when none is active. The project-state-graph suite (`psg`) is the
long one, about 8 minutes. The `live` and `llm_consistency` markers and the manual
arbiter evaluations are deselected by default and never run unattended.

The quickest end-to-end check is the demo — one command, deterministic,
self-verifying. It runs `examples/phantom-uplift`: a revenue number that jumps
+23.2% because an upstream column silently stopped arriving.

```bash
make demo    # 🔴 MISMATCH (column_dropped: promo_discount) → revise → ✅ VERIFIED → SELF-CHECK OK
```

The failure class itself, with numbers, is written up in
[`docs/benchmark-silent-class-drop.md`](docs/benchmark-silent-class-drop.md)
(segment purity 0.31 against 0.91 on the same green pipeline).

### Three levels of verification

| level | command | expect |
|---|---|---|
| quickest — end to end | `make demo` | MISMATCH → revise → VERIFIED, `SELF-CHECK OK`, exit 0 |
| full — every suite | `bash scripts/run_tests.sh` | every suite passes |
| packaging — the pip install case | `bash scripts/test_packaging.sh` (needs `uv`) | wheel **and** sdist each install into a fresh venv and pass the smoke test |
| **release — before every release** | `bash scripts/release-e2e.sh` | four stages from zero in a clean sandbox; exit 0, or every finding read and accepted by a person |

### Before every release — `scripts/release-e2e.sh`

**Run it, and paste its output into the release PR.** A green test run is not
evidence that the release works: two defects walked past one in a single week
and each was found only by doing the thing. `python3 -m venv .venv` — the first
command of §3 — fails on Debian, Ubuntu and WSL, and no test on a development
machine can catch it because the venv is already there. The dashboard did
nothing when clicked on Chrome and Edge for a week while 267 webapp tests
stayed green, because every one of them renders on the server and none drives a
browser.

```bash
bash scripts/release-e2e.sh                 # the release setting: every suite (~20 min)
bash scripts/release-e2e.sh --suites collect  # faster: collect the suites instead of running them
bash scripts/release-e2e.sh --stage 2 --keep  # one stage, and keep the sandbox to look at
```

Four stages, and each does the part the suites structurally cannot reach:

0. **A plugin user.** The plugin installed from a clone of this tree into a
   `HOME` and a Claude configuration of its own, then one cold session: the
   bootstrap builds the venv, `provledger` is on the session's `PATH`, and the
   hooks write their first rows. The dashboard is started before there is any
   ledger, and the agent is given a first task it plans and runs itself.
1. **A stranger's install.** Clone into an empty directory and follow this
   document as written, through `make demo`. Anywhere the script must deviate
   from what is printed here to succeed, it reports a **FINDING** — that is a
   place a new reader is stuck.
2. **A dummy project, then the three surfaces.** A small project whose whole
   history the script writes, so the right answer to every question about it is
   known in advance; then `/ledger` and `/receipts` — once through a stand-in for
   the session, once as **real slash commands** in stage 0's plugin session — and
   the dashboard **clicked by a real Chromium**. It also plants all four kinds of recorded failure and asks
   questions that deliberately avoid their vocabulary, and checks that no
   documented flow forks a second model when nobody asked for one.
3. **A model marks the real sessions' answers** against key points written
   down before the questions were asked — whether the answer is *right*, which
   no existing assertion can tell. Real sessions are nondeterministic and the
   judge is itself a model, so a disagreement is reported as a **FINDING** for a
   person to read, never decided automatically either way.

It is safe to run repeatedly: one temporary directory it creates and removes,
its own `HOME`, its own ledger, its own graph registry and its own
`CLAUDE_CONFIG_DIR`. It never touches `~/skill-workspace/orchestrator.db` or
your registered projects, and it proves that at the end rather than promising it.

Exit code: `0` all green · `2` green but with findings — a place the documents
were deviated from, or an answer the judge marked down · `3` a check could not
run at all · `1` a check ran and said no. `0` ships; `2` ships only once a person
has read every finding and accepted it in the release PR. Stage 3's input and
marking and the real sessions' raw streams are kept outside the sandbox, in
`~/.cache/provledger/release-checks/<run>/`, so the findings can be checked.
Stage 2's browser needs Playwright, which is deliberately not in
`requirements.txt` — a 150 MB browser has no business in a plugin's runtime
dependencies:

```bash
python3 -m pip install playwright && python3 -m playwright install chromium
```

---

## 5b · Install as a Python library (PyPI)

The orchestrator core (plan/step state machine, runtime profiling, drift
detection, decision ledger — stdlib-only) is published on PyPI as
[`provledger`](https://pypi.org/project/provledger/):

```bash
pip install provledger                  # or, from a clone: pip install ./orchestrator-backend
python3 -c "from provledger import api, db; print('ok')"
```

The package on PyPI is the same `provledger` the plugin ships: the ledger and
its tables, the `provledger` command (`why`, `ask`, `receipts`, `plan`, …) and
the migrations. What only the plugin brings is everything that runs inside
Claude Code: the hooks that record your words and the tool calls as you work,
the plan skills, `/ledger` and `/receipts`, and the dashboard. Install from PyPI
to read or write a ledger from your own code, or to run `provledger` against a
ledger the plugin keeps. A PyPI upload is a separate step from a plugin release
and can trail it: `pip show provledger` names the version you have, and the
CHANGELOG says what each version holds.

The wheel ships the SQL migrations inside the package, so
`db.run_migrations()` works from a plain install — no clone needed.

**Packaging/install test case** — build wheel + sdist, install each into a
fresh venv, and run a smoke test (plan/steps, migrations-from-wheel,
profile → drift → decision → ledger) from outside the repo:

```bash
bash scripts/test_packaging.sh   # needs uv; fails loudly on any packaging gap
```

---

## 6 · Launch the provLedger Dashboard

The dashboard is a **read-only** view over an orchestrator SQLite database.

> Needs the `provledger` package installed (§4a). Without it the dashboard still
> starts and still serves 200s, but its graph and `/ledger` pages say "unavailable"
> instead of failing loudly — so a missing §4a looks like a broken dashboard.

```bash
cd orchestrator-webapp
ORCH_DB=~/skill-workspace/orchestrator.db \
    python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Then open <http://127.0.0.1:8765>.

- The DB path is resolved from the **`ORCH_DB`** environment variable, falling back
  to `~/skill-workspace/orchestrator.db`.
- The dashboard opens the DB **read-only**, so it never blocks or changes the
  orchestrator — and the orchestrator works correctly even if the dashboard is
  down. Its one write is a log row (`ask_log`) for each question asked on `/ledger`.

### Run the dashboard in the background

```bash
cd orchestrator-webapp
nohup python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8765 \
    > /tmp/provledger-dashboard.log 2>&1 &
# tail the log:
tail -f /tmp/provledger-dashboard.log
```

---

## 7 · Initialize an orchestrator database (optional)

The database is created and migrated automatically the first time anything opens
it — a hook, a skill script or the `provledger` command. To create one by hand,
use the same code path, which records each migration it applies:

```bash
python3 -c "from provledger import db; c = db.open_db(); db.run_migrations(c); print('ok')"
```

`open_db()` uses `~/skill-workspace/orchestrator.db`; pass a path to put it elsewhere.

---

## 8 · Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: tree_sitter_css` | Install the language grammars (step 4). They are only needed for the Project State Graph analyzers, not the orchestrator. |
| Dashboard shows *"orchestrator.db not found"* | Set `ORCH_DB` to a valid database path, or initialize one (step 7). |
| `tree-sitter` fails to build | Ensure you are on Python 3.11+ so prebuilt wheels are available, or install a C toolchain. |
| Port already in use | Pick another port: `--port 8770`. |
