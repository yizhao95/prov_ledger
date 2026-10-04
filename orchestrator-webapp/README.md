# provLedger dashboard

A localhost page over the provLedger ledger (`ORCH_DB`, default `~/skill-workspace/orchestrator.db`):
the plan that is running and its steps, what each task read and decided, the project map and one
node's history, sessions, outcomes, and a place to ask the ledger a question. It refreshes every
2 seconds. It opens the database read-only; its one write is one `ask_log` row per question asked
on `/ledger`. Add `?lang=zh` to any page for the Chinese UI.

Open it with `bash orchestrator-webapp/launch_dashboard.sh` (or `/provledger-dashboard` inside a
session), then go to <http://127.0.0.1:8765>. Full instructions:
[INSTALL.md §6](../INSTALL.md#6--launch-the-provledger-dashboard).

## Routes

| Route | What it shows |
|---|---|
| `/` | the live plan and its steps |
| `/plan/{id}?node=&at=` | one task: what it read, what it decided, its findings and outcomes |
| `/graph/{project}?focus=&at=&mode=` | the project map, now or at a past run; `mode` is `focus`, `story` (the default without a focus), `data` or `full` |
| `/node/{project}/{qualified_name}?at=&show=` | one node's timeline, reasons, constraints and occurrences |
| `/session/{id}` | what one session said, cost and changed, with or without a plan |
| `/history` | past plans |
| `/search?q=&project=` | recorded words, reasons and constraints only: a substring (`LIKE`) match, newest 40 |
| `/ledger?q=&project=` | ask a question about one project; `project=` is required |
| `/ledger/results` · `/ledger/card?ask_id=` | the answer alone (the form's HTMX target); a logged question's evidence card as markdown |
| `/outcomes?project=` | every claim with its latest outcome, tier, delta and who backfilled it |
| `/api/dashboard?plan=&node=&at=` · `/api/health` | the fragment `/` and `/plan` poll every 2 s; liveness (503 while there is no readable ledger) |

Developers: conventions, tests and the rules this app keeps are in [CLAUDE.md](CLAUDE.md).
Designers: the palette and the React mirror in `design/` are described in
[docs/design.md](../docs/design.md).
