"""FastAPI app — localhost dashboard for the provLedger orchestrator DB.

Routes:
  GET /                 — full dashboard page (initial load)
  GET /api/dashboard    — HTMX partial (auto-refresh target every 2s)
  GET /api/health       — JSON ping for uptime monitoring
  GET /outcomes         — every claim across plans with its latest outcome (phase 8, FL-042)
  GET /node/{project}/{qualified_name} — one node's space / time / intent ledger (phase 8, FL-009)
  GET /graph/{project}?focus=&at=&level= — the project as it is, or was at a run (DP phase 2b)
  GET /session/{session_id} — what a session said, cost, changed, published (DP phase 2b)

Read-only access to ~/skill-workspace/orchestrator.db. Never mutates.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from app import queries

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Make helpers available in templates
TEMPLATES.env.globals["status_badge"] = queries.status_badge
TEMPLATES.env.globals["format_duration"] = queries.format_duration
TEMPLATES.env.globals["type_badge"] = queries.type_badge
TEMPLATES.env.globals["source_badge"] = queries.source_badge
TEMPLATES.env.globals["get_last_n_log_entries"] = queries.get_last_n_log_entries
TEMPLATES.env.globals["count_log_entries"] = queries.count_log_entries
TEMPLATES.env.globals["short_title"] = queries.short_title
TEMPLATES.env.globals["relative_time"] = queries.relative_time
TEMPLATES.env.globals["outcome_badge"] = queries.outcome_badge
TEMPLATES.env.globals["db_path_display"] = queries.db_path_display
TEMPLATES.env.globals["tier_badge"] = queries.tier_badge

app = FastAPI(title="provLedger Dashboard", version="0.1.0")


def _build_context(request: Request, plan_id: str | None = None, node: str | None = None, at: str | None = None) -> dict:
    """Build the template context for one plan.

    If `plan_id` is None, the latest plan is shown (default behavior).
    Otherwise the named plan is loaded — used by /plan/{plan_id} and
    by /api/dashboard?plan=<id>.
    """
    def _error_ctx(msg: str) -> dict:
        return {
            "request": request, "error": msg, "plan": None, "steps": [],
            "skills": [], "completed": 0, "failed": 0, "total_steps": 0,
            "progress_pct": 0, "has_failure": False, "current_step": None,
            "deviations": [], "data_profiles": [], "data_decisions": [],
            "node_reasons": [], "unstated": {"slots": 0, "unstated": 0, "pct": 0}, "outcomes": [],
            "headline": None, "shown_adopted": {"plan": {"shown": [], "adopted": []}, "steps": {}}, "overhead": None,
            "total_plans": 0, "db_size_kb": 0, "viewing_plan_id": plan_id,
            "focus_node": node, "focus_at": at, "bar": queries.view_bar("task", queries.triple(None, node, at), plan_id),
            "plan_session": None, "session_plans": [], "recent_sessions": [],
        }

    try:
        conn = queries.open_db_readonly()
    except FileNotFoundError as e:
        return _error_ctx(f"orchestrator.db not found: {e}. Have you run any plans yet?")
    except sqlite3.Error as e:  # DASH-BUG2: locked/corrupt DB degrades, not 500s
        return _error_ctx(f"database error: {e}")

    # DASH-BUG2: any query below can raise sqlite3.OperationalError ("database is
    # locked") while the orchestrator writes. Degrade to an error context instead
    # of a 500. conn is always closed (BUG1: no leaked handles on any path).
    try:
        plan = queries.get_plan_by_id(conn, plan_id) if plan_id else queries.get_latest_plan(conn)
        if plan_id and not plan:
            # Asked for a specific plan that doesn't exist — reuse conn (BUG1).
            total_plans = queries.count_total_plans(conn)
            db_size_kb = queries.get_db_size_kb()
            ctx = _error_ctx(f"Plan not found: {plan_id}")
            ctx["total_plans"] = total_plans
            ctx["db_size_kb"] = db_size_kb
            return ctx
        steps = queries.get_steps_for_plan(conn, plan["plan_id"]) if plan else []
        # Convert flat list → tree (parent→children) with parallel-group annotations.
        steps_tree = queries.build_step_tree(steps) if steps else []
        skills = queries.get_skills_for_plan(conn, plan["plan_id"]) if plan else []
        completed = queries.count_completed_steps(steps)
        failed = sum(1 for s in steps if s["status"] == "FAILED")
        current_step = next((s for s in steps if s["status"] == "IN_PROGRESS"), None)  # UX2
        deviations = queries.get_deviations(conn, plan["plan_id"]) if plan else []     # UX4
        # Phase 5.2: read-only data panel — profile snapshots + LLM decision trail.
        data_profiles = queries.get_data_profiles(conn, plan["plan_id"]) if plan else []
        data_decisions = queries.get_data_decisions(conn, plan["plan_id"]) if plan else []
        # Phase 3: close-time reasons (read-only) + the unstated gap.
        node_reasons = queries.get_node_reasons(conn, plan["plan_id"]) if plan else []
        unstated = queries.get_unstated(conn, plan["plan_id"]) if plan else {"slots": 0, "unstated": 0, "pct": 0}
        # Phase 7: what the plan's expectations turned into (read-only).
        plan_outcomes = queries.get_outcomes(conn, plan["plan_id"]) if plan else []
        # DP phase 2 (Task 7): the plan headline, what was shown / adopted per step, and the overhead numbers.
        headline = queries.get_headline(conn, plan["plan_id"]) if plan else None
        shown_adopted = queries.get_shown_adopted(conn, plan["plan_id"]) if plan else {"plan": {"shown": [], "adopted": []}, "steps": {}}
        overhead = queries.get_overhead(conn, plan["plan_id"]) if plan else None
        total_plans = queries.count_total_plans(conn)
        db_size_kb = queries.get_db_size_kb()
        # DP phase 2b (Task 5): the plan's session and its other plans; the home page's recent sessions
        plan_session = plan.get("session_id") if plan else None
        session_plans = [dict(r) for r in conn.execute("SELECT plan_id, status FROM Plans WHERE session_id = ? AND plan_id <> ? ORDER BY created_at", (plan_session, plan["plan_id"]))] if plan_session else []
        recent_sessions = queries.recent_sessions(conn) if not plan_id else []
    except sqlite3.Error as e:
        return _error_ctx(f"database error: {e}")
    finally:
        conn.close()

    return {
        "request": request,
        "error": None,
        "plan": plan,
        "steps": steps_tree,
        "steps_flat": steps,   # kept for any downstream code that expects flat
        "skills": skills,
        "completed": completed,
        "failed": failed,
        "has_failure": failed > 0,
        "current_step": current_step,
        "deviations": deviations,
        "data_profiles": data_profiles,
        "data_decisions": data_decisions,
        "node_reasons": node_reasons,
        "unstated": unstated,
        "outcomes": plan_outcomes,
        "headline": headline,
        "shown_adopted": shown_adopted,
        "overhead": overhead,
        "total_steps": len(steps),
        "progress_pct": int(100 * completed / len(steps)) if steps else 0,
        "total_plans": total_plans,
        "db_size_kb": db_size_kb,
        "viewing_plan_id": plan_id,  # None = viewing latest; set = viewing a specific historical plan
        # DP phase 2b (Task 4): the context triple this page carries and the view bar built from it
        "focus_node": node,
        "focus_at": at,
        "bar": queries.view_bar("task", queries.triple(plan.get("project") if plan else None, node, at), plan["plan_id"] if plan else None),
        "plan_session": plan_session,
        "session_plans": session_plans,
        "recent_sessions": recent_sessions,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    """Full dashboard page — latest plan. HTMX inside auto-refreshes the partial."""
    context = _build_context(request)
    return TEMPLATES.TemplateResponse(request, "dashboard.html", context)


@app.get("/plan/{plan_id}", response_class=HTMLResponse)
def view_plan(request: Request, plan_id: str, node: str | None = None, at: str | None = None):
    """View a specific historical plan by id (linked from /history). `node` / `at`
    are the context triple (DP phase 2b): the node is highlighted, the bar keeps both."""
    context = _build_context(request, plan_id=plan_id, node=node, at=at)
    return TEMPLATES.TemplateResponse(request, "dashboard.html", context)


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    """List all past plans — user_query as title (or plan_id fallback)."""
    try:
        conn = queries.open_db_readonly()
    except FileNotFoundError as e:
        return TEMPLATES.TemplateResponse(request, "history.html", {
            "request": request,
            "error": f"orchestrator.db not found: {e}",
            "plans": [],
        })
    plans = queries.list_all_plans(conn)
    conn.close()
    return TEMPLATES.TemplateResponse(request, "history.html", {
        "request": request,
        "error": None,
        "plans": plans,
        "bar": queries.view_bar("history", queries.triple(None, None, None)),
    })


@app.get("/api/dashboard", response_class=HTMLResponse)
def dashboard_partial(request: Request, plan: str | None = None, node: str | None = None, at: str | None = None):
    """HTMX partial — swappable inner content.

    Optional ?plan=<plan_id> query param: poll a specific historical plan
    instead of the latest one.

    Uses HTTP ETag + 304 Not Modified so HTMX skips the swap entirely
    when the DB hasn't changed since the last poll. ETag still hashes
    ALL plans/steps so any DB change invalidates — safe for the latest-
    plan view AND the historical-plan view (a historical plan is mostly
    immutable so 304s dominate even more).
    """
    # 1. Cheap signature first — if it matches the client's If-None-Match,
    #    short-circuit BEFORE rendering the template.
    try:
        conn = queries.open_db_readonly()
        try:
            etag = queries.compute_etag(conn)
        finally:
            conn.close()
    except (FileNotFoundError, sqlite3.Error):
        etag = '"no-db"'

    # ETag varies by which plan we're showing — prefix with plan_id (or 'latest')
    plan_key = plan if plan else "latest"
    etag = f'"{plan_key}:{etag.strip(chr(34))}"'

    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": "no-store, must-revalidate",
            },
        )

    # 2. Otherwise render the partial and stamp the new ETag
    context = _build_context(request, plan_id=plan, node=node, at=at)
    response = TEMPLATES.TemplateResponse(request, "_dashboard_partial.html", context)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/api/health")
def health():
    """JSON ping. Useful for `curl` smoke tests + monitoring."""
    try:
        conn = queries.open_db_readonly()
        try:
            plan = queries.get_latest_plan(conn)
        finally:
            conn.close()
        return JSONResponse({
            "ok": True,
            "latest_plan_id": plan["plan_id"] if plan else None,
            "latest_status": plan["status"] if plan else None,
        })
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "orchestrator.db not found"}, status_code=503)
    except sqlite3.Error as e:  # DASH-BUG2: locked/corrupt DB → structured 503
        return JSONResponse({"ok": False, "error": f"database error: {e}"}, status_code=503)


@app.get("/outcomes", response_class=HTMLResponse)
def outcomes(request: Request, project: str | None = None):
    """Phase 8 (FL-042): every expectation across plans with its latest
    outcome — a claim ledger. Read-only; an old DB renders an empty page."""
    ctx = {"request": request, "error": None, "rows": [], "stats": queries.outcome_stats([]), "project": project,
           "bar": queries.view_bar("outcomes", queries.triple(project, None, None))}
    try:
        conn = queries.open_db_readonly()
    except FileNotFoundError as e:
        ctx["error"] = f"orchestrator.db not found: {e}"
        return TEMPLATES.TemplateResponse(request, "outcomes.html", ctx)
    try:
        rows = queries.get_expectations_with_latest_outcome(conn, project=project)
        all_rows = rows if not project else queries.get_expectations_with_latest_outcome(conn)
    except sqlite3.Error as e:
        ctx["error"] = f"database error: {e}"
        return TEMPLATES.TemplateResponse(request, "outcomes.html", ctx)
    finally:
        conn.close()
    stats = queries.outcome_stats(rows)
    stats["projects"] = queries.outcome_stats(all_rows)["projects"]
    ctx.update(rows=rows, stats=stats)
    return TEMPLATES.TemplateResponse(request, "outcomes.html", ctx)


@app.get("/session/{session_id}", response_class=HTMLResponse)
def session_card(request: Request, session_id: str):
    """DP phase 2b (Task 5, FL-062): one session — what was said, what it cost,
    what changed, its headline, the plans it published; a session without a
    plan is marked 降级. Read-only; an older DB renders empty parts."""
    ctx = {"request": request, "error": None, "session": None, "bar": queries.view_bar("session", queries.triple(None, None, None))}
    try:
        conn = queries.open_db_readonly()
    except FileNotFoundError as e:
        ctx["error"] = f"orchestrator.db not found: {e}"
        ctx["session"] = {"session_id": session_id, "found": False, "degraded": False, "run": None, "utterances": [], "tool_calls": 0, "buckets": {}, "ratios": {}, "changed_nodes": [], "headline": None, "plans": []}
        return TEMPLATES.TemplateResponse(request, "session.html", ctx)
    try:
        ctx["session"] = queries.get_session(conn, session_id)
        if ctx["session"]["run"] and ctx["session"]["run"].get("project"):
            ctx["bar"] = queries.view_bar("session", queries.triple(ctx["session"]["run"]["project"], None, None))
    except sqlite3.Error as e:
        ctx["error"] = f"database error: {e}"
        ctx["session"] = {"session_id": session_id, "found": False, "degraded": False, "run": None, "utterances": [], "tool_calls": 0, "buckets": {}, "ratios": {}, "changed_nodes": [], "headline": None, "plans": []}
    finally:
        conn.close()
    return TEMPLATES.TemplateResponse(request, "session.html", ctx)


@app.get("/graph/{project}", response_class=HTMLResponse)
def graph_view(request: Request, project: str, focus: str | None = None, at: str | None = None, level: str = "functions"):
    """DP phase 2b (Task 3): the project as it is (or was, at a run) — nodes with a
    badge (records with a story) and the tier of their latest event. PSG only
    through psg_bridge; a missing graph is 200 + "state graph unavailable"."""
    import json as _json
    ctx = {"request": request, "error": None, "project": project, "focus": focus or None, "graph": None, "graph_json": "{}",
           "bar": queries.view_bar("graph", queries.triple(project, focus, at))}
    try:
        conn = queries.open_db_readonly()
    except FileNotFoundError as e:
        ctx["error"] = f"orchestrator.db not found: {e}"
        ctx["graph"] = {"available": False, "reason": ctx["error"], "nodes": [], "edges": [], "runs": [], "run": None, "level": level, "badged": 0}
        return TEMPLATES.TemplateResponse(request, "graph.html", ctx)
    try:
        ctx["graph"] = queries.get_graph(conn, project, at=at, level=level)
        ctx["graph_json"] = _json.dumps({"nodes": ctx["graph"]["nodes"], "edges": ctx["graph"]["edges"]}, default=str)
    except sqlite3.Error as e:
        ctx["error"] = f"database error: {e}"
        ctx["graph"] = {"available": False, "reason": ctx["error"], "nodes": [], "edges": [], "runs": [], "run": None, "level": level, "badged": 0}
    finally:
        conn.close()
    return TEMPLATES.TemplateResponse(request, "graph.html", ctx)


@app.get("/node/{project}/{qualified_name:path}", response_class=HTMLResponse)
def node_ledger(request: Request, project: str, qualified_name: str):
    """Phase 8 (FL-009): one node's upstream/downstream, history and reasons —
    three dimensions in one read-only query (docs/NORTH-STAR essence #2)."""
    at = request.query_params.get("at")
    ctx = {"request": request, "error": None, "ledger": None, "project": project, "qualified_name": qualified_name,
           "at": at,                                       # DP phase 2: ?at=<reason_id> highlights one record; 2b: a run id highlights the run
           "bar": queries.view_bar("node", queries.triple(project, qualified_name, at))}
    try:
        conn = queries.open_db_readonly()
    except FileNotFoundError as e:
        ctx["error"] = f"orchestrator.db not found: {e}"
        return TEMPLATES.TemplateResponse(request, "node.html", ctx)
    try:
        ctx["ledger"] = queries.get_node_ledger(conn, project, qualified_name)
    except sqlite3.Error as e:
        ctx["error"] = f"database error: {e}"
    finally:
        conn.close()
    return TEMPLATES.TemplateResponse(request, "node.html", ctx)
