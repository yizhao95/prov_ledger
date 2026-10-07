# provLedger

A Claude Code plugin plus a stdlib-only Python package (`provledger`) that records why code and data
changed and reads it back (`README.md` says what it does for users). The repo root is the plugin
(`.claude-plugin/`, source `"./"`): `skills/`, `commands/` and `hooks/hooks.json` are auto-discovered
and shipped to every user.

## Map
- `orchestrator-backend/`: core library; source dir `orchestrator/` ships as package `provledger`.
- `orchestrator-webapp/`: FastAPI + Jinja2 + HTMX dashboard.
- `skills/`: plan skills, the project-state-graph analyzer and its review driver, ledger, receipts,
  and superpowers-derived prompt skills.
- `hooks/`: `_hook.sh` → `python -m orchestrator.hooks <Event>`; always exits 0.
- `bin/provledger`: on the session's PATH while the plugin is enabled; hands every call to the
  plugin venv's `provledger`.
- `scripts/`: `bootstrap.sh` (builds the venv), `suites.sh` + `run_tests.sh`, `release-e2e.sh`,
  `test_packaging.sh`.
- `examples/phantom-uplift/`: the demo (`make demo`).

The backend, webapp, skills/ and the two state-graph skills each have their own CLAUDE.md.

## Environment
Two venvs, never mixed:
- **Dev:** `PY=.venv/bin/python` in the repo root (Python 3.13; `provledger` is an editable install of
  this working tree). Create it with `uv venv --python 3.13 .venv && VIRTUAL_ENV=.venv uv pip install -r
  requirements.txt`, from the repo root. `run_tests.sh` picks it up (`--which` shows the choice).
- **Plugin runtime:** `~/skill-workspace/.venv`, built by the installed plugin's `bootstrap.sh`; its
  `provledger` is the installed release. Never install into it from the working tree.
- The installed plugin comes from a release tag (`claude plugin marketplace add
  yizhao95/prov_ledger#vX.Y.Z`), never from this checkout, so live sessions do not run half-edited code.
- In a git worktree the dev venv's `provledger` still points at the main checkout. Shadow it:
  `mkdir pp && ln -s "$PWD/orchestrator-backend/orchestrator" pp/provledger && PYTHONPATH=pp …`.
- System `python3` has no deps. `skills/project-state-graph/scripts` also has its own uv env (`uv sync`).

## Tests
`bash scripts/run_tests.sh [suite…]` runs each suite in its own pytest process (combining suites in
one pytest command fails spuriously) and fails any suite that writes to the real `~/skill-workspace`
(`scripts/home_guard.py`). `--list` names them, `--count` counts them; the suite list lives only in
`scripts/suites.sh`. Test counts in any document come from `--count`, never from
memory. There is no CI: run what you touched before calling it done. Never run the suites and
`release-e2e.sh` at the same time: wall-clock assertions turn machine load into failures. When the e2e
goes red, first ask whether the product or the harness failed.
- A retrieval fix is proven end to end: run the read a person would run and check the needle is in
  its output. Green unit tests have missed four "read, then dropped by the renderer" defects.

## Rules
- One piece of knowledge lives in one place; elsewhere, link to it. User-facing docs: `README.md`
  (front page only), `INSTALL.md`, `docs/`. Dev conventions: these CLAUDE.md files.
- Product changes, docs and wording are checked against `docs/NORTH-STAR.md`. Gate verdicts and
  failure reasons state what was observed; they never tell the reader what to do.
- Every git-tracked file is English (no CJK), including CLAUDE.md. Exceptions are listed with
  reasons in `scripts/tests/test_repo_language.py`.
- `README.md` is pinned by `scripts/tests/test_readme_v2.py`; the command table lives in
  `docs/cli.md`, checked against `orchestrator/cli.py`.
- Outside `analyzer/_host.py`, no line may contain both `sys.path.insert` and `orchestrator-backend`
  (repo-wide check in the PSG suite's `test_host_import.py`).
- Examples in shipped files (docs, SKILL.md, docstrings, tests) are generic. A real case from the
  ledger is an answer key for the local eval (`local-eval/`) and must not ship.
- Experiments and audits run on a copy of the ledger: `why` and `ask` append access-log rows. The
  ledger is in WAL, so copy it with `orchestrator.db.copy_ledger` (SQLite's backup API), never
  `cp`. Read SQLite with Python's `sqlite3` module (`?mode=ro`); the `sqlite3` CLI may not be installed.
- Anything run by hand that touches a DB: point `ORCH_DB` and `PSG_REGISTRY_ROOT` at temp paths
  first, or it writes to the real ledger (see README §4). Never hand-edit ledger tables.
- Release: bump the version in every file `orchestrator-backend/tests/test_version.py` checks, add a
  CHANGELOG entry, branch `release/x.y.z`, tag `vX.Y.Z`. `bash scripts/release-e2e.sh` must exit 0
  and its output goes in the release PR (INSTALL.md §5).
- Local-only (gitignored): `docs/superpowers/` (old plans and specs; history, not current truth),
  `docs/internal/` (`FUTURE-LOG.md`: deferred work as FL-nnn rows; add, mark DONE with the PR, never
  delete), `local-eval/`. User-facing limitations go in `docs/KNOWN-ISSUES.md`.
