# orchestrator-webapp

Read-only dashboard: FastAPI + Jinja2 + HTMX, no Node at runtime. Routes in `app/main.py`,
all SQL in `app/queries.py`, every user-facing word in `app/vocab.py`.

## Commands (`PY=~/skill-workspace/.venv/bin/python`)
- Test: `bash scripts/run_tests.sh webapp` (from repo root).
- Run: `bash launch_dashboard.sh` (:8765), or from this dir
  `ORCH_DB=<db> ~/skill-workspace/.venv/bin/uvicorn app.main:app --port 8765`
- Colours: edit `app/static/tokens.json`, then `python3 scripts/gen_tokens.py` (`--check` to verify).
  Never hand-edit `app/static/tokens.js` or `design/src/tokens.ts`.

## Rules
- GET/HEAD routes only; DB opened `mode=ro` (both tested). The one write is `queries.log_ask`
  (one `ask_log` row per `/ledger` query). Add no others.
- Import the backend as `provledger.*`, never `orchestrator` (tested).
- `ORCH_DB` is read at import time: tests set it, then `importlib.reload(queries); importlib.reload(main)`.
  Seed helpers: `tests/test_routes.py`, `orchestrator-backend/tests/_psg_schema.py`.
- Queries swallow `sqlite3.Error` and return empty, so a backend schema change shows up as a silently
  empty panel. Run this suite after any migration or view change.
- A new table shown on the dashboard must be added to `queries.compute_etag`, or the 2 s poll stays 304.
- New phrases go in `vocab.py` in both `en` and `zh`; `data-*` attributes keep raw ledger tokens.
  Chinese only in `vocab.py` and the templates whitelisted in `scripts/tests/test_repo_language.py`.
- An internal link that should keep `?lang=` goes through `|keep_lang(lang)`, and every page context
  carries `lang` (the view bar, its search form and the poll's `poll_url` already do).
- Keep the DOM hooks `scripts/release_e2e/browser_check.py` clicks: `hx-boost` on body, `data-mode-chip`,
  `data-view-link`, `data-panel="ledger"`, `form[hx-get="/ledger/results"]`. htmx is 1.9.10 (no `htmx.swap`).
- Route paths are also built in `orchestrator-backend/orchestrator/ask/__init__.py::_url`: change both.
- A running dashboard does not reload code, and the launchers only probe `/api/health`: after webapp
  changes, `pkill -f "[u]vicorn app.main"` and relaunch.
- Rendered attributes are HTML-escaped (`&amp;`): `html.unescape` an href before asserting on it.
  `base.html`'s view bar also carries `data-at`, so scope `?at=` assertions to `data-record` elements.
- `tests/test_tokens.py` briefly rewrites `app/static/tokens.json` in the working tree.

## design/
React mirror of the components for Claude Design sync (`.design-sync/config.json`).
`npm ci && npm run build && npm test`. `dist/` is committed, and `npm test` fails unless it is
byte-identical to a fresh build of `src/`.
Never rename props; they are `queries.py` return shapes.
