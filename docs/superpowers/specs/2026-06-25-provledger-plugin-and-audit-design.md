# provLedger — Plugin Packaging + Audit-Driven Improvement (Design)

**Date:** 2026-06-25
**Status:** Approved (design); pending spec review
**Owner:** yzhao950213@gmail.com
**Repo:** github.com/yizhao95/prov_ledger

---

## 1 · Goal

Make provLedger **trivially installable** as a Claude Code plugin (one-command
install + zero-touch dependency setup), then **audit and improve** the codebase —
without changing core behavior — and **contribute everything back** via pull
requests against the repo the maintainer owns.

Three driving constraints from the maintainer:

1. **The dashboard is first-class.** It is the primary surface for reviewing the
   coding agent's work and MUST be bundled and one-command launchable.
2. **Do not ship duplicate skills.** Skills that merely copy upstream Superpowers
   become a soft dependency on the `superpowers` plugin; only evolved/novel skills
   are bundled.
3. **Core semantics are frozen; quality is fair game.** The state machine,
   migrations, and contract gates keep their behavior. Code readability, dashboard
   readability/UX, and the quality of the harness content itself (skill prompts,
   instructions) are explicit improvement targets.

---

## 2 · Scope & Decomposition

This effort is delivered in three phases that share context but produce
independent, reviewable artifacts.

| Phase | Name | Nature | Deliverable |
|---|---|---|---|
| **P0** | Code audit | read-only | A graded findings list (`docs/.../audit-findings.md`), risk × value. Feeds P1 and P2. |
| **P1** | Plugin packaging | implementation | One-command-installable plugin (manifest + marketplace + auto-bootstrap + dashboard launcher). Its own PR. |
| **P2** | Optimization | implementation | The subset of P0 findings the maintainer selects. Its own PR. |

**Order:** P0 audit → P1 packaging → P2 optimization → full test pass + PRs.

P0 runs first because its findings inform both the packaging (what to fix while
restructuring) and P2 (what to optimize). P1 ships before P2 because it is the
highest-value, best-bounded change.

**Non-goals (YAGNI):**
- No change to the orchestrator state-machine semantics, migration contracts, or
  the code/data contract-gate logic.
- No new runtime services beyond the existing dashboard.
- No publishing to npm / external registries; distribution is via the GitHub
  marketplace entry only.

---

## 3 · P1 — Plugin Packaging Design

### 3.1 Plugin root

The repository root already contains `skills/`, so the repo root **is** the plugin
root. No file relocation required for skills. New additions:

```
prov_ledger/
├── .claude-plugin/
│   ├── plugin.json          # plugin manifest
│   └── marketplace.json     # marketplace entry (single-plugin marketplace)
├── scripts/
│   ├── bootstrap.sh         # idempotent dependency self-setup
│   └── pl-python            # interpreter resolver used by skill scripts
├── hooks/
│   └── hooks.json           # SessionStart -> bootstrap (async, no-op when warm)
├── commands/
│   └── provledger-dashboard.md   # one-command dashboard launcher
├── requirements.txt         # pinned dependency set (single source of truth)
└── ... (existing orchestrator-backend/, orchestrator-webapp/, skills/, CLI)
```

### 3.2 Install UX (the payoff)

```
/plugin marketplace add yizhao95/prov_ledger
/plugin install provledger@provledger
```

On first session after install, the `SessionStart` hook runs `bootstrap.sh` in the
background. After that the user just uses the skills; `/provledger-dashboard`
launches the dashboard.

### 3.3 Dependency bootstrap (the core of "reduce install difficulty")

- **Single source of truth:** `requirements.txt` with **pinned** versions
  (pytest, fastapi, uvicorn[standard], jinja2, tree-sitter + grammars). Backend is
  stdlib-only; deps exist for the dashboard, the graph analyzers, and tests.
- **`scripts/bootstrap.sh`** — idempotent:
  1. Resolve venv dir: `${PROVLEDGER_VENV:-$HOME/skill-workspace/.venv}`.
  2. If a **marker file** keyed on the SHA-256 of `requirements.txt` already
     matches → exit 0 immediately (warm path, sub-10ms).
  3. Else create the venv (`uv venv` if `uv` is on PATH, else `python3 -m venv`),
     install pinned deps (`uv pip install` / `pip install`), write the marker.
  4. All output to a log; never block on failure beyond a clear message.
- **`scripts/pl-python`** — tiny wrapper that prints/execs the bootstrapped venv's
  python (`$PROVLEDGER_VENV/bin/python`), falling back to system `python3` with a
  warning if the venv is absent. Skill scripts call `pl-python` instead of bare
  `python`/`python3`.
- **`hooks/hooks.json`** — `SessionStart` runs
  `${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap.sh` with `async: true`. Warm sessions
  are a no-op; the first session sets up in the background without blocking.

**Default venv location:** `~/skill-workspace/.venv`. Rationale: the project
already centralizes runtime state under `~/skill-workspace/` (default DB
`~/skill-workspace/orchestrator.db`); keeping the venv there co-locates all
provLedger runtime state, lives outside the repo (re-clones don't duplicate it,
git never tracks it), and is overridable via `PROVLEDGER_VENV`.

### 3.4 Dashboard as a bundled first-class citizen

- `orchestrator-webapp/` is bundled as-is.
- New `commands/provledger-dashboard.md` slash command launches it using the
  bootstrapped venv, honoring `ORCH_DB` (default `~/skill-workspace/orchestrator.db`),
  on a configurable port, backgrounded with a tailable log — wrapping the existing
  `launch_dashboard.sh` so there is no logic duplication.

### 3.5 Skills: bundle vs. declare-dependency

For each skill in `skills/`, diff against the locally installed official
`superpowers` plugin:

- **Byte/near-identical supporting skills** — `brainstorming`,
  `systematic-debugging`, `test-driven-development`,
  `verification-before-completion`, `subagent-driven-development`:
  **NOT bundled.** Instead, declare a **soft dependency** on `superpowers`
  (documented in README + plugin metadata: "install superpowers for the full
  experience"). References to them degrade gracefully when superpowers is absent.
- **Evolved / novel skills** — `writing-plans`, `executing-plans` (evolved from
  Superpowers, wired to the orchestrator backend), `project-state-graph`,
  `update-project-state-graph`: **bundled.**

Plugin skills are namespaced (`provledger:writing-plans`), so even bundled skills
never hard-collide with `superpowers:*`.

The exact identical-vs-diverged determination is produced during P0 (a real diff),
and the bundle list is finalized from that evidence — not assumed here.

### 3.6 Acceptance criteria (P1)

- Fresh-environment smoke: from a clean state, `marketplace add` + `install` +
  first-session bootstrap yields a working venv with all pinned deps.
- All bundled skill scripts run via the bootstrapped venv.
- `/provledger-dashboard` serves the dashboard against a DB.
- Warm `SessionStart` adds no perceptible startup latency.
- All 6 test suites still pass (see §6).

---

## 4 · P0 — Audit Design

Read-only, fan-out across the four subsystems (orchestrator-backend,
orchestrator-webapp, orchestrator-cli, skills). Produced by parallel read-only
subagents; results consolidated into `docs/superpowers/specs/2026-06-25-audit-findings.md`
graded by **risk × value**, each finding tagged with phase (fix-in-P1 vs P2).

Audit dimensions:

1. **Correctness bugs** — especially the core invariants: state transitions,
   migrations 001–009, circuit breakers, atomic log capture (PIPESTATUS hardening),
   immutable COMPLETED steps.
2. **Dead code / duplication / oversized files / tangled boundaries.**
3. **Performance** — analyzer graph traversals, SQL query patterns, N+1 reads.
4. **Test gaps** — uncovered branches around the invariants above.
5. **Security** — SQL construction, path handling, subprocess usage in shell
   wrappers.
6. **Readability** — code clarity AND dashboard readability/UX.
7. **Harness quality** — clarity and correctness of the skill prompts/instructions
   themselves.

The maintainer selects which findings proceed to P2. Core semantics stay frozen;
everything else is a candidate.

---

## 5 · P2 — Optimization Design

Implement only the maintainer-selected P0 findings. Each change is TDD-guarded
(failing test → fix → green) and must not alter frozen core semantics. Internal
interfaces may change if external behavior is preserved and tests are updated to
match.

---

## 6 · Testing & Contribute-Back

**Baseline (must stay green throughout):**

```
orchestrator-backend                                  -> 111 passed
orchestrator-webapp                                   ->  11 passed
skills/writing-plans/tests                            ->  44 passed
skills/executing-plans                                ->  50 passed
skills/project-state-graph/scripts/tests              -> 198 passed, 1 skipped
skills/update-project-state-graph/scripts/tests       ->  50 passed
                                              TOTAL    -> 464 passed, 1 skipped
```

All runs use the bootstrapped venv. P1 additionally runs a clean-environment
install smoke test.

**Contribute back:** the maintainer owns the repo, so this is feature-branch →
commit → `gh pr create`. Separate PRs for P1 and P2 for clean review. Requires `gh`
authenticated with push rights to `yizhao95/prov_ledger`; if not, the maintainer
runs `gh auth login` (via `!` in-session) before the PR step.

---

## 7 · Risks

| Risk | Mitigation |
|---|---|
| Bootstrap slows every session | Marker-keyed warm path exits in ms; hook is async. |
| tree-sitter wheels fail on a platform | Pinned versions known to ship wheels for 3.11+; bootstrap surfaces a clear message and falls back. |
| Editing skill scripts to use `pl-python` breaks tests | TDD per change; full suite is the gate. |
| Dropping a "duplicate" skill that had local edits | Decision driven by a real diff in P0, not assumption. |
| Plugin root / `${CLAUDE_PLUGIN_ROOT}` path assumptions | Smoke test from a clean install validates real paths. |
