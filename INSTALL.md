# Installation Guide

This guide walks you through installing **provLedger** from scratch, verifying the
install, and launching the dashboard.

---

## 0 · Fastest path — install as a Claude Code plugin

```text
/plugin marketplace add yizhao95/prov_ledger
/plugin install provledger@provledger
```

Dependencies install themselves on first session (a background `SessionStart`
bootstrap builds one venv at `~/skill-workspace/.venv` and installs
`requirements.txt`; warm sessions are a no-op). Launch the dashboard with
`/provledger-dashboard`.

**Companion (recommended):** install the
[`superpowers`](https://github.com/obra/superpowers) plugin. provLedger treats it
as a **soft dependency** — the byte-identical `verification-before-completion`
skill is not bundled and comes from superpowers when present; the other shared
skills are bundled with local adaptations. Everything still works without
superpowers, just with fewer supporting process skills.

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
git clone git@github.com:yizhao95/prov_ledger.git
cd prov_ledger
```

---

## 3 · Create a virtual environment

### Option A — standard library `venv`

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

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
| Test suites | `pytest` |

Install everything:

```bash
# with pip
pip install pytest fastapi "uvicorn[standard]" jinja2 \
    tree-sitter tree-sitter-css tree-sitter-html \
    tree-sitter-javascript tree-sitter-python

# or with uv
uv pip install pytest fastapi "uvicorn[standard]" jinja2 \
    tree-sitter tree-sitter-css tree-sitter-html \
    tree-sitter-javascript tree-sitter-python
```

> **Minimal install (orchestrator only, no graph, no dashboard):**
> the backend needs nothing beyond Python + `pytest` for the tests.

---

## 5 · Verify the install

Run each test suite **separately** (each has its own pyproject/pythonpath —
one combined invocation breaks). A healthy install passes all of them:

```bash
python -m pytest orchestrator-backend                  -q   # 152 passed
python -m pytest orchestrator-webapp                   -q   #  23 passed
python -m pytest skills/writing-plans/tests            -q   #  46 passed, 2 skipped
python -m pytest skills/executing-plans                -q   #  52 passed
python -m pytest skills/project-state-graph/scripts/tests     -q   # 232 passed, 1 skipped
python -m pytest skills/update-project-state-graph/scripts/tests -q   #  55 passed
```

Total: **560 passed, 3 skipped**.

The quickest end-to-end check is the demo — one command, deterministic,
self-verifying:

```bash
make demo    # v1 MISMATCH (purity 0.31) → revise → v2 VERIFIED (0.91)
```

---

## 5b · Install as a Python library (PyPI)

The orchestrator core (plan/step state machine, runtime profiling, drift
detection, decision ledger — stdlib-only) is published on PyPI as
[`provledger`](https://pypi.org/project/provledger/):

```bash
pip install provledger
python -c "from provledger import api, db; print('ok')"
```

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

```bash
cd orchestrator-webapp
ORCH_DB=~/skill-workspace/orchestrator.db \
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Then open <http://127.0.0.1:8765>.

- The DB path is resolved from the **`ORCH_DB`** environment variable, falling back
  to `~/skill-workspace/orchestrator.db`.
- The dashboard opens the DB in **WAL read-only** mode, so it never blocks or mutates
  the orchestrator — and the orchestrator works correctly even if the dashboard is
  down.

### Run the dashboard in the background

```bash
cd orchestrator-webapp
nohup python -m uvicorn app.main:app --host 127.0.0.1 --port 8765 \
    > /tmp/provledger-dashboard.log 2>&1 &
# tail the log:
tail -f /tmp/provledger-dashboard.log
```

---

## 7 · Initialize an orchestrator database (optional)

The database is created/migrated automatically the first time you publish a plan
through the `writing-plans` skill. To create one manually from the migrations:

```bash
python - <<'PY'
import sqlite3, glob, pathlib
db = pathlib.Path.home() / "skill-workspace" / "orchestrator.db"
db.parent.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(db)
for f in sorted(glob.glob("orchestrator-backend/orchestrator/migrations/*.sql")):
    conn.executescript(open(f).read())
conn.commit()
print("Initialized", db)
PY
```

---

## 8 · Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: tree_sitter_css` | Install the language grammars (step 4). They are only needed for the Project State Graph analyzers, not the orchestrator. |
| Dashboard shows *"orchestrator.db not found"* | Set `ORCH_DB` to a valid database path, or initialize one (step 7). |
| `tree-sitter` fails to build | Ensure you are on Python 3.11+ so prebuilt wheels are available, or install a C toolchain. |
| Port already in use | Pick another port: `--port 8770`. |
