# P1 — Plugin Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make provLedger installable as a Claude Code plugin with one marketplace command and zero-touch dependency setup, with the dashboard bundled and one-command launchable.

**Architecture:** The repo root already *is* the plugin root (`skills/` is auto-discovered). We add plugin metadata (`.claude-plugin/`), a single consolidated `requirements.txt`, an idempotent `scripts/bootstrap.sh` that builds one unified venv at `~/skill-workspace/.venv`, a `scripts/pl-python` interpreter resolver, a `SessionStart` hook that runs bootstrap asynchronously, and a `/provledger-dashboard` command. Existing skill scripts already contain interpreter-resolution loops; we prepend the unified venv as the first candidate (keeping every existing fallback), so the change is additive and low-risk.

**Tech Stack:** Bash, Python 3.13 (stdlib + pytest), JSON (plugin/marketplace manifests), FastAPI/uvicorn (existing dashboard), tree-sitter (existing analyzers).

## Global Constraints

- **Migration-safe only:** never edit applied migrations 001–009; schema changes are new forward migrations (010+). (No schema changes in P1, but the rule holds.)
- **Green suite is the gate:** baseline is **464 passed, 1 skipped** across the 6 suites; it must stay green.
- **Unified venv path:** `${PROVLEDGER_VENV:-$HOME/skill-workspace/.venv}` — single source of truth for the interpreter, overridable by `PROVLEDGER_VENV`.
- **Plugin name / marketplace name:** `provledger` (install target `provledger@provledger`).
- **Plugin root references:** runtime paths inside hooks/commands use `${CLAUDE_PLUGIN_ROOT}`.
- **No network assumptions in unit tests:** tests must not require `pip install`; the real dependency install is covered by the manual clean-install smoke test (Task 9).
- **Additive script edits:** when wiring the unified venv into existing resolver loops, prepend the new candidate; never remove an existing fallback.

---

### Task 1: Static packaging metadata (manifest + marketplace + requirements)

**Files:**
- Create: `.claude-plugin/plugin.json`
- Create: `.claude-plugin/marketplace.json`
- Create: `requirements.txt`
- Test: `tests/test_plugin_metadata.py`
- Create: `tests/__init__.py` (empty, so the new top-level `tests/` is importable)

**Interfaces:**
- Consumes: nothing.
- Produces: `requirements.txt` (consumed by Task 2 bootstrap); plugin name `provledger` and marketplace name `provledger` (consumed by Task 8 docs).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plugin_metadata.py
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def test_plugin_manifest_valid():
    data = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert data["name"] == "provledger"
    assert data["version"]
    assert data["description"]
    # skills are auto-discovered from skills/; manifest must not be empty
    assert "author" in data

def test_marketplace_lists_provledger():
    data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert data["name"] == "provledger"
    names = [p["name"] for p in data["plugins"]]
    assert "provledger" in names
    entry = next(p for p in data["plugins"] if p["name"] == "provledger")
    # plugin source is the marketplace root itself
    assert entry["source"] == "./"

def test_requirements_has_all_runtime_deps():
    reqs = (ROOT / "requirements.txt").read_text().lower()
    for pkg in ["pytest", "fastapi", "uvicorn", "jinja2",
                "tree-sitter", "tree-sitter-css", "tree-sitter-html",
                "tree-sitter-javascript", "tree-sitter-python"]:
        assert pkg in reqs, f"{pkg} missing from requirements.txt"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_plugin_metadata.py -v`
Expected: FAIL — `.claude-plugin/plugin.json` does not exist (FileNotFoundError).

- [ ] **Step 3: Write the metadata files**

```json
// .claude-plugin/plugin.json
{
  "name": "provledger",
  "version": "0.1.0",
  "description": "Provenance + ledger discipline for data-science coding agents: a SQLite plan/step orchestrator, a project state graph with code+data contract gates, and a live review dashboard. Pairs with the 'superpowers' plugin (optional but recommended).",
  "author": { "name": "yzhao", "email": "yzhao950213@gmail.com" },
  "homepage": "https://github.com/yizhao95/prov_ledger",
  "license": "MIT"
}
```

```json
// .claude-plugin/marketplace.json
{
  "name": "provledger",
  "owner": { "name": "yzhao", "email": "yzhao950213@gmail.com" },
  "plugins": [
    {
      "name": "provledger",
      "source": "./",
      "description": "Provenance + ledger discipline for data-science coding agents."
    }
  ]
}
```

```text
# requirements.txt — single consolidated dependency set for provLedger.
# Constraints match the validated pyproject floors (Python 3.13, prebuilt wheels).
# orchestrator-backend itself is stdlib-only; these cover the dashboard,
# the project-state-graph analyzers, and the test suites.
pytest>=7
fastapi>=0.110
uvicorn[standard]>=0.27
jinja2>=3.1
tree-sitter>=0.25.2
tree-sitter-css>=0.25.0
tree-sitter-html>=0.23.2
tree-sitter-javascript>=0.25.0
tree-sitter-python>=0.25.0
```

Also create empty `tests/__init__.py`:

```python
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_plugin_metadata.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add .claude-plugin/plugin.json .claude-plugin/marketplace.json requirements.txt tests/__init__.py tests/test_plugin_metadata.py
git commit -m "feat(plugin): add plugin manifest, marketplace entry, consolidated requirements"
```

---

### Task 2: Idempotent dependency bootstrap

**Files:**
- Create: `scripts/bootstrap.sh`
- Test: `tests/test_bootstrap.py`

**Interfaces:**
- Consumes: `requirements.txt` (Task 1).
- Produces: a venv at `${PROVLEDGER_VENV:-$HOME/skill-workspace/.venv}` and a marker file `<venv>/.provledger-reqs.sha256` whose content is the SHA-256 of `requirements.txt`. The warm path (marker matches) exits 0 without invoking the installer. Honors `PROVLEDGER_BOOTSTRAP_INSTALLER` to override the install command (used by tests to avoid network).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bootstrap.py
import hashlib, os, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "scripts" / "bootstrap.sh"

def _run(env_extra, tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), **env_extra}
    return subprocess.run(["bash", str(BOOTSTRAP)], env=env,
                          capture_output=True, text=True)

def test_cold_run_invokes_installer_and_writes_marker(tmp_path):
    venv = tmp_path / "venv"
    sentinel = tmp_path / "installer-ran"
    # Stub installer: just touch a sentinel + create the venv dir; no network.
    installer = f'mkdir -p "{venv}/bin" && touch "{sentinel}"'
    r = _run({"PROVLEDGER_VENV": str(venv),
              "PROVLEDGER_BOOTSTRAP_INSTALLER": installer}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert sentinel.exists(), "installer was not invoked on cold run"
    marker = venv / ".provledger-reqs.sha256"
    expected = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
    assert marker.read_text().strip() == expected

def test_warm_run_skips_installer(tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir(parents=True)
    expected = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
    (venv / ".provledger-reqs.sha256").write_text(expected)
    sentinel = tmp_path / "installer-ran"
    installer = f'touch "{sentinel}"'
    r = _run({"PROVLEDGER_VENV": str(venv),
              "PROVLEDGER_BOOTSTRAP_INSTALLER": installer}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert not sentinel.exists(), "installer ran despite matching marker"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bootstrap.py -v`
Expected: FAIL — `scripts/bootstrap.sh` does not exist.

- [ ] **Step 3: Write the bootstrap script**

```bash
#!/usr/bin/env bash
# bootstrap.sh — idempotent dependency self-setup for the provLedger plugin.
# Builds one unified venv and installs requirements.txt. A marker keyed on the
# SHA-256 of requirements.txt makes warm runs a no-op (safe to call on every
# SessionStart).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQS="${PLUGIN_ROOT}/requirements.txt"
VENV="${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}"
MARKER="${VENV}/.provledger-reqs.sha256"
LOG="${PROVLEDGER_BOOTSTRAP_LOG:-/tmp/provledger-bootstrap.log}"

if [[ ! -f "${REQS}" ]]; then
    echo "❌ bootstrap: requirements.txt not found at ${REQS}" >&2
    exit 1
fi

want="$(sha256sum "${REQS}" | awk '{print $1}')"

# Warm path: marker matches -> nothing to do.
if [[ -f "${MARKER}" ]] && [[ "$(cat "${MARKER}" 2>/dev/null)" == "${want}" ]]; then
    exit 0
fi

mkdir -p "$(dirname "${VENV}")"

# Test/override hook: a custom installer command replaces venv creation + pip.
if [[ -n "${PROVLEDGER_BOOTSTRAP_INSTALLER:-}" ]]; then
    bash -c "${PROVLEDGER_BOOTSTRAP_INSTALLER}" >>"${LOG}" 2>&1
    rc=$?
else
    {
        if command -v uv >/dev/null 2>&1; then
            uv venv "${VENV}" && \
            VIRTUAL_ENV="${VENV}" uv pip install -r "${REQS}"
        else
            python3 -m venv "${VENV}" && \
            "${VENV}/bin/python" -m pip install --upgrade pip && \
            "${VENV}/bin/python" -m pip install -r "${REQS}"
        fi
    } >>"${LOG}" 2>&1
    rc=$?
fi

if [[ ${rc} -ne 0 ]]; then
    echo "❌ bootstrap: dependency install failed (see ${LOG})" >&2
    exit "${rc}"
fi

echo "${want}" > "${MARKER}"
exit 0
```

Make it executable:

```bash
chmod +x scripts/bootstrap.sh
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_bootstrap.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/bootstrap.sh tests/test_bootstrap.py
git commit -m "feat(plugin): idempotent dependency bootstrap with marker-keyed warm path"
```

---

### Task 3: Interpreter resolver (`pl-python`)

**Files:**
- Create: `scripts/pl-python`
- Test: `tests/test_pl_python.py`

**Interfaces:**
- Consumes: `${PROVLEDGER_VENV:-$HOME/skill-workspace/.venv}`.
- Produces: an executable `scripts/pl-python` that execs the unified venv's python (`<venv>/bin/python`) with all passed args; if that python is absent, falls back to system `python3` and prints a one-line warning to stderr.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pl_python.py
import os, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PL = ROOT / "scripts" / "pl-python"

def test_uses_unified_venv_python(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    fake = venv / "bin" / "python"
    fake.write_text('#!/usr/bin/env bash\necho FAKE_VENV_PY "$@"\n')
    fake.chmod(0o755)
    env = {**os.environ, "PROVLEDGER_VENV": str(venv)}
    r = subprocess.run(["bash", str(PL), "-c", "ignored"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "FAKE_VENV_PY" in r.stdout

def test_falls_back_to_system_python3_with_warning(tmp_path):
    venv = tmp_path / "nonexistent-venv"
    env = {**os.environ, "PROVLEDGER_VENV": str(venv)}
    r = subprocess.run(["bash", str(PL), "-c", "print('hi')"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "hi"
    assert "warning" in r.stderr.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pl_python.py -v`
Expected: FAIL — `scripts/pl-python` does not exist.

- [ ] **Step 3: Write the resolver**

```bash
#!/usr/bin/env bash
# pl-python — resolve the provLedger interpreter and exec it with all args.
# Prefers the unified venv python; falls back to system python3 with a warning.
set -uo pipefail

VENV="${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}"
VENV_PY="${VENV}/bin/python"

if [[ -x "${VENV_PY}" ]]; then
    exec "${VENV_PY}" "$@"
fi

SYS_PY="$(command -v python3 || true)"
if [[ -n "${SYS_PY}" ]]; then
    echo "⚠️  pl-python: unified venv python not found at ${VENV_PY}; falling back to ${SYS_PY}. Run scripts/bootstrap.sh to set up dependencies." >&2
    exec "${SYS_PY}" "$@"
fi

echo "❌ pl-python: no python interpreter found (venv or system)." >&2
exit 1
```

Make it executable:

```bash
chmod +x scripts/pl-python
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pl_python.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/pl-python tests/test_pl_python.py
git commit -m "feat(plugin): pl-python interpreter resolver with system fallback"
```

---

### Task 4: Wire the unified venv into existing skill-script resolvers

**Files:**
- Modify (prepend unified-venv candidate to the `for _cand in ...` loop):
  - `skills/writing-plans/scripts/publish-plan.sh`
  - `skills/writing-plans/scripts/ledger-add.sh`
  - `skills/executing-plans/scripts/append-log.sh`
  - `skills/executing-plans/scripts/fail-step.sh`
  - `skills/executing-plans/scripts/finish-plan.sh`
  - `skills/executing-plans/scripts/run-step.sh`
  - `skills/executing-plans/scripts/agent-review-close.sh`
  - `skills/executing-plans/scripts/complete-step.sh`
  - `skills/executing-plans/scripts/deviate.sh`
  - `skills/executing-plans/scripts/record-skill.sh`
  - `skills/executing-plans/scripts/start-step.sh`
- Test: `tests/test_resolver_wiring.py`

**Interfaces:**
- Consumes: `${PROVLEDGER_VENV:-$HOME/skill-workspace/.venv}/bin/python`.
- Produces: every listed script's resolver loop tries the unified venv first, then its existing fallbacks unchanged.

Each script currently contains this exact line as the first loop candidate:

```bash
    for _cand in "${SCRIPT_DIR}/../../../.venv/bin/python" "${HOME}/skill-workspace/orchestrator/.venv/bin/python" "$(command -v python3 || true)"; do
```

The edit inserts the unified venv as the new first candidate:

```bash
    for _cand in "${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}/bin/python" "${SCRIPT_DIR}/../../../.venv/bin/python" "${HOME}/skill-workspace/orchestrator/.venv/bin/python" "$(command -v python3 || true)"; do
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resolver_wiring.py
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = [
    "skills/writing-plans/scripts/publish-plan.sh",
    "skills/writing-plans/scripts/ledger-add.sh",
    "skills/executing-plans/scripts/append-log.sh",
    "skills/executing-plans/scripts/fail-step.sh",
    "skills/executing-plans/scripts/finish-plan.sh",
    "skills/executing-plans/scripts/run-step.sh",
    "skills/executing-plans/scripts/agent-review-close.sh",
    "skills/executing-plans/scripts/complete-step.sh",
    "skills/executing-plans/scripts/deviate.sh",
    "skills/executing-plans/scripts/record-skill.sh",
    "skills/executing-plans/scripts/start-step.sh",
]
UNIFIED = '${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}/bin/python'

def test_all_resolvers_prefer_unified_venv():
    for rel in SCRIPTS:
        text = (ROOT / rel).read_text()
        assert UNIFIED in text, f"{rel} does not reference the unified venv"
        # existing system fallback must remain
        assert 'command -v python3' in text, f"{rel} dropped its python3 fallback"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_resolver_wiring.py -v`
Expected: FAIL — unified venv string absent from the scripts.

- [ ] **Step 3: Apply the edit to all 11 scripts**

Run this exact in-place edit (matches the shared line verbatim, inserts the unified candidate first):

```bash
for f in \
  skills/writing-plans/scripts/publish-plan.sh \
  skills/writing-plans/scripts/ledger-add.sh \
  skills/executing-plans/scripts/append-log.sh \
  skills/executing-plans/scripts/fail-step.sh \
  skills/executing-plans/scripts/finish-plan.sh \
  skills/executing-plans/scripts/run-step.sh \
  skills/executing-plans/scripts/agent-review-close.sh \
  skills/executing-plans/scripts/complete-step.sh \
  skills/executing-plans/scripts/deviate.sh \
  skills/executing-plans/scripts/record-skill.sh \
  skills/executing-plans/scripts/start-step.sh ; do
  perl -0pi -e 's{for _cand in "\$\{SCRIPT_DIR\}/\.\./\.\./\.\./\.venv/bin/python"}{for _cand in "\$\{PROVLEDGER_VENV:-\$\{HOME\}/skill-workspace/.venv\}/bin/python" "\$\{SCRIPT_DIR\}/../../../.venv/bin/python"}g' "$f"
done
```

After running, manually verify one file shows the four-candidate loop (Task asserts via the test in Step 4).

- [ ] **Step 4: Run wiring test + the affected skill suites to confirm no regression**

Run: `python -m pytest tests/test_resolver_wiring.py skills/writing-plans/tests skills/executing-plans -v`
Expected: PASS — wiring test green; writing-plans 44 passed; executing-plans 50 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_resolver_wiring.py skills/writing-plans/scripts skills/executing-plans/scripts
git commit -m "feat(plugin): prefer unified venv in skill-script interpreter resolvers"
```

---

### Task 5: Unify dashboard launcher on the unified venv + add `/provledger-dashboard`

**Files:**
- Modify: `orchestrator-webapp/launch_dashboard.sh` (venv path + configurable port/app-dir)
- Create: `commands/provledger-dashboard.md`
- Test: `tests/test_dashboard_command.py`

**Interfaces:**
- Consumes: `${PROVLEDGER_VENV:-$HOME/skill-workspace/.venv}/bin/uvicorn`; `ORCH_DB`; `PROVLEDGER_DASH_PORT` (default 8765); `${CLAUDE_PLUGIN_ROOT}`.
- Produces: a launcher that uses the unified venv and a configurable port; a slash command that runs the launcher against the bundled webapp.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard_command.py
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def test_launcher_uses_unified_venv_and_configurable_port():
    text = (ROOT / "orchestrator-webapp" / "launch_dashboard.sh").read_text()
    assert "PROVLEDGER_VENV" in text, "launcher not pointed at unified venv"
    assert "PROVLEDGER_DASH_PORT" in text, "launcher port not configurable"

def test_dashboard_command_exists_and_invokes_launcher():
    cmd = ROOT / "commands" / "provledger-dashboard.md"
    text = cmd.read_text()
    assert text.startswith("---"), "command file missing frontmatter"
    assert "launch_dashboard.sh" in text
    assert "CLAUDE_PLUGIN_ROOT" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dashboard_command.py -v`
Expected: FAIL — `PROVLEDGER_VENV` not in launcher; command file missing.

- [ ] **Step 3a: Edit `orchestrator-webapp/launch_dashboard.sh`**

Replace the configuration block (lines 17–21, the `PORT`/`APP_DIR`/`VENV_PY`/`LOG` definitions) with:

```bash
PORT="${PROVLEDGER_DASH_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"
# App dir: prefer the bundled webapp (plugin root), else the legacy workspace copy.
APP_DIR="${PROVLEDGER_WEBAPP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
VENV="${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}"
VENV_PY="${VENV}/bin/uvicorn"
LOG="${PROVLEDGER_DASH_LOG:-/tmp/webapp-server.log}"
```

(The original line 18 already defined `URL`; the block above keeps `URL` defined once. Verify no duplicate `URL=` remains.) Also update the fix-it hint on line ~39 to reference bootstrap:

```bash
    echo "   Run: bash \"${CLAUDE_PLUGIN_ROOT:-<plugin-root>}/scripts/bootstrap.sh\" to install dependencies."
```

- [ ] **Step 3b: Create the slash command**

```markdown
---
description: Launch the read-only provLedger review dashboard (bundled webapp) on localhost.
---

Launch the provLedger dashboard using the plugin's bundled webapp and the unified venv.

Run this command:

```bash
PROVLEDGER_WEBAPP_DIR="${CLAUDE_PLUGIN_ROOT}/orchestrator-webapp" \
  bash "${CLAUDE_PLUGIN_ROOT}/orchestrator-webapp/launch_dashboard.sh"
```

Then report the dashboard URL to the user. The dashboard reads the orchestrator
SQLite DB **read-only** (path from `ORCH_DB`, default `~/skill-workspace/orchestrator.db`).
Override the port with `PROVLEDGER_DASH_PORT` if 8765 is taken.
```

- [ ] **Step 4: Run test + the webapp suite to confirm no regression**

Run: `python -m pytest tests/test_dashboard_command.py orchestrator-webapp -v`
Expected: PASS — command test green; webapp 11 passed.

- [ ] **Step 5: Commit**

```bash
git add orchestrator-webapp/launch_dashboard.sh commands/provledger-dashboard.md tests/test_dashboard_command.py
git commit -m "feat(plugin): unify dashboard launcher on shared venv + add /provledger-dashboard"
```

---

### Task 6: SessionStart hook running bootstrap asynchronously

**Files:**
- Create: `hooks/hooks.json`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap.sh` (Task 2).
- Produces: a `SessionStart` hook that runs bootstrap with `async: true`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hooks.py
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def test_sessionstart_runs_bootstrap_async():
    data = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    entries = data["hooks"]["SessionStart"]
    cmds = [h for e in entries for h in e["hooks"]]
    boot = [c for c in cmds if "bootstrap.sh" in c.get("command", "")]
    assert boot, "no SessionStart hook runs bootstrap.sh"
    assert all(c["type"] == "command" for c in boot)
    assert any(c.get("async") is True for c in boot), "bootstrap hook is not async"
    assert any("CLAUDE_PLUGIN_ROOT" in c["command"] for c in boot)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hooks.py -v`
Expected: FAIL — `hooks/hooks.json` does not exist.

- [ ] **Step 3: Write the hook config**

```json
// hooks/hooks.json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap.sh\"",
            "async": true,
            "statusMessage": "provLedger: ensuring dependencies"
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hooks.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add hooks/hooks.json tests/test_hooks.py
git commit -m "feat(plugin): SessionStart hook bootstraps dependencies asynchronously"
```

---

### Task 7: Skill bundle decision — drop superpowers duplicates, declare soft dependency

**Files:**
- Possibly delete (only if the diff proves substantial identity with installed superpowers): `skills/brainstorming/`, `skills/systematic-debugging/`, `skills/test-driven-development/`, `skills/verification-before-completion/`, `skills/subagent-driven-development/`
- Create: `docs/superpowers/specs/2026-06-25-skill-diff.md` (evidence record)
- Test: `tests/test_skill_bundle.py`

**Interfaces:**
- Consumes: locally installed superpowers at `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.0.3/skills/`.
- Produces: the final bundled-skill set; a recorded decision per skill.

- [ ] **Step 1: Produce the diff evidence**

Run (records, per shared skill, whether SKILL.md is identical to the installed superpowers copy):

```bash
SP="${HOME}/.claude/plugins/cache/claude-plugins-official/superpowers/6.0.3/skills"
{
  echo "# Skill diff vs superpowers 6.0.3 — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for s in brainstorming systematic-debugging test-driven-development verification-before-completion subagent-driven-development; do
    echo
    echo "## ${s}"
    if [[ -f "${SP}/${s}/SKILL.md" ]]; then
      if diff -q "skills/${s}/SKILL.md" "${SP}/${s}/SKILL.md" >/dev/null 2>&1; then
        echo "RESULT: IDENTICAL -> drop, declare dependency"
      else
        echo "RESULT: DIVERGED -> keep bundled"
        echo '```diff'
        diff "${SP}/${s}/SKILL.md" "skills/${s}/SKILL.md" | head -60
        echo '```'
      fi
    else
      echo "RESULT: not present in superpowers -> keep bundled"
    fi
  done
} > docs/superpowers/specs/2026-06-25-skill-diff.md
cat docs/superpowers/specs/2026-06-25-skill-diff.md
```

- [ ] **Step 2: Act on the evidence**

For each skill marked `IDENTICAL`, delete its directory:

```bash
# Run ONLY for skills the diff marked IDENTICAL. Example:
git rm -r skills/brainstorming
```

For any marked `DIVERGED` or `not present`, leave it bundled. (The four evolved/novel skills — writing-plans, executing-plans, project-state-graph, update-project-state-graph — are never touched here.)

- [ ] **Step 3: Write the test encoding the final decision**

```python
# tests/test_skill_bundle.py
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"

# These are always bundled — evolved or novel, never duplicates of superpowers.
ALWAYS_BUNDLED = [
    "writing-plans", "executing-plans",
    "project-state-graph", "update-project-state-graph",
]

def test_core_skills_are_bundled():
    for s in ALWAYS_BUNDLED:
        assert (SKILLS / s / "SKILL.md").exists(), f"{s} must stay bundled"

def test_dropped_duplicates_are_gone():
    # Read the recorded decision; every skill marked IDENTICAL must be absent.
    decision = (ROOT / "docs/superpowers/specs/2026-06-25-skill-diff.md").read_text()
    for block in decision.split("## ")[1:]:
        name = block.splitlines()[0].strip()
        if "RESULT: IDENTICAL" in block:
            assert not (SKILLS / name).exists(), f"{name} marked IDENTICAL but still bundled"
```

- [ ] **Step 4: Run tests + a full skill-suite regression**

Run: `python -m pytest tests/test_skill_bundle.py skills/writing-plans/tests skills/executing-plans skills/project-state-graph/scripts/tests skills/update-project-state-graph/scripts/tests -q`
Expected: PASS — bundle test green; the four bundled suites unchanged (44 + 50 + 198/1skip + 50).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(plugin): drop superpowers-duplicate skills, declare soft dependency (evidence recorded)"
```

---

### Task 8: README/INSTALL rewrite for one-command install

**Files:**
- Modify: `README.md` (Installation section)
- Modify: `INSTALL.md` (lead with plugin install; keep manual path as fallback)
- Test: `tests/test_docs_install.py`

**Interfaces:**
- Consumes: plugin name/marketplace from Task 1; superpowers dependency from Task 7.
- Produces: docs describing the one-command install.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_docs_install.py
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def test_readme_documents_plugin_install():
    text = (ROOT / "README.md").read_text()
    assert "/plugin marketplace add yizhao95/prov_ledger" in text
    assert "/plugin install provledger@provledger" in text

def test_install_md_mentions_superpowers_dependency():
    text = (ROOT / "INSTALL.md").read_text().lower()
    assert "superpowers" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_docs_install.py -v`
Expected: FAIL — install commands not present.

- [ ] **Step 3: Edit the docs**

In `README.md`, replace the `## 🚀 Installation` body's `git clone ...` quick start with the plugin-first flow (keep the manual venv path beneath it as "Manual / development install"):

```markdown
## 🚀 Installation

### Recommended — install as a Claude Code plugin (one command)

```text
/plugin marketplace add yizhao95/prov_ledger
/plugin install provledger@provledger
```

Dependencies install themselves on first session (a background `SessionStart`
bootstrap builds one venv at `~/skill-workspace/.venv`). Launch the review
dashboard any time with `/provledger-dashboard`.

> **Companion:** install the [`superpowers`](https://github.com/obra/superpowers)
> plugin for the full set of supporting process skills (brainstorming,
> systematic-debugging, TDD, …). provLedger bundles only its evolved and novel
> skills and treats superpowers as a recommended soft dependency.

### Manual / development install

(unchanged steps below — clone, venv, pip install, run tests)
```

In `INSTALL.md`, add a short top section mirroring the plugin install and the
superpowers companion note, above the existing manual guide.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_docs_install.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add README.md INSTALL.md tests/test_docs_install.py
git commit -m "docs: lead with one-command plugin install + superpowers companion note"
```

---

### Task 9: Full regression + clean-install smoke test

**Files:**
- Create: `scripts/smoke_install.sh` (clean-room bootstrap exerciser)
- Test: run all suites (no new test file; this task is verification).

**Interfaces:**
- Consumes: all prior tasks.
- Produces: evidence that the baseline holds and a cold bootstrap succeeds.

- [ ] **Step 1: Run the full baseline suite**

Run:

```bash
python -m pytest \
  orchestrator-backend orchestrator-webapp \
  skills/writing-plans/tests skills/executing-plans \
  skills/project-state-graph/scripts/tests \
  skills/update-project-state-graph/scripts/tests \
  tests \
  -q
```

Expected: the 6 original suites still total **464 passed, 1 skipped**, plus the new `tests/` packaging suite all green. (If Task 7 dropped duplicate skills, no test suite is removed — none of the dropped skills had tests.)

- [ ] **Step 2: Write the clean-install smoke script**

```bash
#!/usr/bin/env bash
# smoke_install.sh — exercise a cold bootstrap into a throwaway venv.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
echo "Cold bootstrap into ${TMP}/venv ..."
PROVLEDGER_VENV="${TMP}/venv" bash "${PLUGIN_ROOT}/scripts/bootstrap.sh"
test -x "${TMP}/venv/bin/python" || { echo "❌ venv python missing"; exit 1; }
"${TMP}/venv/bin/python" -c "import fastapi, jinja2, tree_sitter; print('✅ deps import OK')"
echo "Warm re-run (must be a no-op) ..."
PROVLEDGER_VENV="${TMP}/venv" bash "${PLUGIN_ROOT}/scripts/bootstrap.sh"
echo "✅ smoke install passed"
```

- [ ] **Step 3: Run the smoke script** (requires network for the real pip install)

Run: `bash scripts/smoke_install.sh`
Expected: `✅ deps import OK` then `✅ smoke install passed`.

- [ ] **Step 4: Commit**

```bash
git add scripts/smoke_install.sh
git commit -m "test(plugin): clean-install smoke script for cold bootstrap"
```

- [ ] **Step 5: Open the P1 pull request**

```bash
git push -u origin feat/plugin-packaging-and-audit
gh pr create --title "P1: package provLedger as a one-command Claude Code plugin" \
  --body "Adds plugin manifest + marketplace, idempotent dependency bootstrap (unified venv), pl-python resolver, SessionStart hook, bundled dashboard + /provledger-dashboard, and drops superpowers-duplicate skills in favor of a soft dependency. Baseline 464 passed/1 skipped preserved; clean-install smoke added.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

(Requires `gh` authenticated with push rights; if not, the maintainer runs `gh auth login` first.)

---

## Self-Review

**Spec coverage:** §3.1 plugin root (Tasks 1,6); §3.2 install UX (Tasks 1,8); §3.3 bootstrap/venv/pl-python/hook (Tasks 2,3,6); §3.4 dashboard first-class (Task 5); §3.5 bundle-vs-dependency (Task 7); §3.6 acceptance (Task 9); §6 testing + PR (Task 9). All covered.

**Placeholder scan:** No TBD/TODO. The only deliberately conditional step is Task 7 Step 2 (delete *iff* the diff proves identity) — this is evidence-gated by design, with the exact diff command and a test that enforces consistency between the recorded decision and the filesystem.

**Type/name consistency:** `PROVLEDGER_VENV` default `~/skill-workspace/.venv` used identically in bootstrap.sh, pl-python, resolver edits, and launcher. Marker file `.provledger-reqs.sha256` consistent between Task 2 script and test. Plugin/marketplace name `provledger` consistent across Tasks 1 and 8. Command file path `commands/provledger-dashboard.md` consistent in Task 5 script and test.
