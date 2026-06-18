# 🧠🐶 Orchestrator Webapp

A tiny localhost dashboard that renders the latest plan from the **provLedger
orchestrator** SQLite database in real-time.

> **What this is for:** When the orchestrator is mid-execution, you want to
> *see* what's happening — which step is running, how long it's been running,
> what the live log says, when it errors out. This webapp gives you a browser
> tab that auto-refreshes every 2 seconds and always shows the most relevant
> plan, switching automatically when a new plan starts.

---

## ✨ What it does

- Reads `~/skill-workspace/orchestrator.db` **read-only** with **WAL mode**
  enabled, so it never blocks the writer (orchestrator-cli)
- Always displays the **single most relevant plan**, picked by this priority:
  1. Most recent **IN_PROGRESS** plan
  2. Most recent **PENDING** plan
  3. Most recent **COMPLETED** or **FAILED** plan (so the dashboard never
     goes blank between runs)
- Auto-refreshes every **2 seconds** via HTMX polling — no JavaScript build
  step, no SSE / WebSocket complexity
- When a brand-new plan starts in another terminal, the dashboard
  **hot-swaps** to it within 2 seconds without a manual refresh
- Shows: plan goal, status badge, revision count, progress bar, every step
  with status / duration / depth indent, and the live log of the
  currently-running (or most recently logged) step

---

## 🖼️ What you see

```
┌─────────────────────────────────────────────────────────────────────┐
│  🧾 provLedger Dashboard                 ● LIVE — refresh 2s  │
├─────────────────────────────────────────────────────────────────────┤
│  📋 orchestrator-webapp-mvp-20260428161311           🚧 IN_PROGRESS │
│      Build orchestrator-webapp MVP: FastAPI + HTMX...               │
│      Started 16:13:11   ·   Revisions 1/5   ·   Steps 9/12          │
│      ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░  75%                              │
├───┬──────────────────────────────────────┬──────────────┬──────────┤
│ A │ Project skeleton + uv venv           │ ✅ COMPLETED │ 14s      │
│ B │ Install dependencies                 │ ✅ COMPLETED │ 8s       │
│ C │ Write app/queries.py                 │ ✅ COMPLETED │ 23s      │
│ D │ Write app/main.py                    │ ✅ COMPLETED │ 15s      │
│ E │ base.html (tailwind + htmx + brand)│ ✅ COMPLETED │ 4s       │
│ F │ dashboard.html                       │ ✅ COMPLETED │ 0s       │
│ G │ _dashboard_partial.html              │ ✅ COMPLETED │ 0s       │
│ H │ Smoke test                           │ ✅ COMPLETED │ 47s      │
│ I │ Live test                            │ 🚧 RUNNING…  │ 12s…     │
│ J │ Write README                         │ ⏳ PENDING   │ —        │
│ K │ Copy to repo                         │ ⏳ PENDING   │ —        │
│ L │ Git commit + push                    │ ⏳ PENDING   │ —        │
├─────────────────────────────────────────────────────────────────────┤
│ 📜 Live log — step I                                                │
│ ─────────────────────────────────────────────────────────────────── │
│ [16:21:03] 🚧 Starting step I                                       │
│ [16:21:08] 🌐 open http://127.0.0.1:8765 issued                     │
│ [16:21:09] ✅ Browser opened, server PID 20274 confirmed            │
└─────────────────────────────────────────────────────────────────────┘
        7 total plans in DB · 64 KB
```

---

## 🚀 Quick start

```bash
# 1. Install dependencies (one-time)
cd orchestrator-webapp
uv venv
uv pip install fastapi 'uvicorn[standard]' jinja2

# 2. Run the server
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765

# 3. Open in your browser
open http://127.0.0.1:8765    # macOS
xdg-open http://127.0.0.1:8765 # Linux
start http://127.0.0.1:8765    # Windows
```

The dashboard will be empty until you've created a plan via the
`orchestrator-cli.py` (which lives in the orchestrator package, separate from
this webapp).

### Run in the background

```bash
nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 \
  > /tmp/webapp-server.log 2>&1 &
echo "Webapp PID: $!"
tail -f /tmp/webapp-server.log
```

To stop:
```bash
lsof -ti:8765 | xargs kill -9
```

---

## 🛠️ Tech stack

| Layer    | Choice                  | Why                                            |
|----------|-------------------------|------------------------------------------------|
| Web      | **FastAPI** + **uvicorn** | Tiny + async-ready          |
| Templates| **Jinja2**              | Built into Starlette / FastAPI                 |
| Frontend | **HTMX 1.9** (CDN)      | Reactive polling without JS build step         |
| Styling  | **Tailwind CSS** (CDN)  | Fast prototyping; brand palette via config   |
| Storage  | **SQLite (read-only + WAL)** | Concurrent reads while orchestrator writes |
| Colors   | Brand palette | blue.100 / spark.100 / green.100 / red.100    |

---

## 🎨 Brand colors used

| Color           | Hex       | Where                     |
|-----------------|-----------|---------------------------|
| blue.100        | `#0053e2` | Header bar + plan id      |
| spark.100       | `#ffc220` | IN_PROGRESS badge bg      |
| green.100       | `#2a8703` | COMPLETED badge + progress|
| red.100         | `#ea1100` | FAILED badge + errors     |
| gray.10/50/100  | various   | Borders, subtle text      |

All combinations meet **WCAG 2.2 AA** contrast (4.5:1 text, 3:1 UI).
`prefers-reduced-motion` is respected (pulses disabled).

---

## 📂 Project layout

```
orchestrator-webapp/
├── pyproject.toml           # uv + deps
├── README.md                # ← you are here
├── .gitignore
└── app/
    ├── __init__.py
    ├── main.py              # FastAPI app: /, /api/dashboard, /api/health
    ├── queries.py           # Read-only SQLite queries + helpers
    └── templates/
        ├── base.html        # Layout (Tailwind + HTMX + brand palette)
        ├── dashboard.html   # Full page; HTMX polls inner partial
        └── _dashboard_partial.html  # Swappable inner content
```

Total: **~250 lines of Python + ~150 of HTML/Tailwind**.

---

## 🌐 API

| Route             | Returns                                                    |
|-------------------|------------------------------------------------------------|
| `GET /`           | Full HTML dashboard page (initial load)                    |
| `GET /api/dashboard` | HTML partial — inner content for HTMX swap (every 2s)   |
| `GET /api/health` | JSON `{ok, latest_plan_id, latest_status}`                 |

Example:
```bash
curl -s http://127.0.0.1:8765/api/health | jq
# {
#   "ok": true,
#   "latest_plan_id": "orchestrator-webapp-mvp-20260428161311",
#   "latest_status": "IN_PROGRESS"
# }
```

---

## ⚠️ Known limitations (honest list)

| Limitation | Severity | Notes |
|------------|----------|-------|
| **2-second polling latency** | 🟢 green | Indistinguishable from real-time for human watching; can upgrade to SSE later if needed |
| **`log_context` is a sliding window** (50 lines / 4000 chars max per step) | 🟡 yellow | Inherits the orchestrator's Phase 4 audit gap — older log entries fall off |
| **No transition history view** | 🟡 yellow | Only current state shown; no PENDING→IN_PROGRESS→COMPLETED audit trail (would need a `StepTransitions` event table) |
| **Read-only — no controls** | 🟢 green | Can't pause/cancel/retry from the UI; out of scope for "watch" use case |
| **Localhost only — no auth** | 🟢 green | Bound to `127.0.0.1`. Don't expose to LAN without adding auth |
| **Auto-switch loses focus on plan-change** | 🟡 yellow | If you're reading old logs and a new plan starts, view jumps. Future: "📌 Pin this plan" toggle |
| **Single user friendly, not multi-tenant** | 🟢 green | SQLite + 2s polling scales to ~50 concurrent viewers; designed for one |
| **No mobile-optimized layout** | 🟢 green | Desktop-first; works on mobile but not designed for it |
| **No dark mode** | 🟢 green | Brand palette has dark variants ready — easy v2 upgrade |

---

## 🔐 Security notes

- Server binds to `127.0.0.1` only (loopback). It is **not** reachable from
  other machines on your network unless you change the `--host` flag.
- DB connection uses SQLite URI mode `file:...?mode=ro` — physically cannot
  write back to the DB even if a bug tried to.
- No user input is ever written anywhere. All routes are GETs.
- HTMX + Tailwind are loaded from public CDNs — pin specific versions if
  your security policy requires (see `base.html`).

---

## 🐾 Why this exists

Without this dashboard, observing the orchestrator means manually running
`sqlite3` queries or tailing markdown files that may be out of sync. That
hides bugs (the original Phase 4 dogfood failure was invisible for several
edits because the markdown drifted from the DB silently). A live view of the
DB makes the orchestrator's state **visible** and **trustworthy**.

This is intentionally a tiny MVP. It is not trying to be a full APM suite —
just a friendly window into the database for a developer watching their own
orchestration runs.

---

## 🪪 License

MIT licensed. See repository root.
