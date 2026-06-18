"""FastAPI app — localhost dashboard for the provLedger orchestrator DB.

Routes:
  GET /                 — full dashboard page (initial load)
  GET /api/dashboard    — HTMX partial (auto-refresh target every 2s)
  GET /api/health       — JSON ping for uptime monitoring

Read-only access to ~/skill-workspace/orchestrator.db. Never mutates.
"""
from __future__ import annotations

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

app = FastAPI(title="provLedger Dashboard", version="0.1.0")


def _build_context(request: Request, plan_id: str | None = None) -> dict:
    """Build the template context for one plan.

    If `plan_id` is None, the latest plan is shown (default behavior).
    Otherwise the named plan is loaded — used by /plan/{plan_id} and
    by /api/dashboard?plan=<id>.
    """
    try:
        conn = queries.open_db_readonly()
    except FileNotFoundError as e:
        return {
            "request": request,
            "error": f"orchestrator.db not found: {e}. Have you run any plans yet?",
            "plan": None,
            "steps": [],
            "skills": [],
            "total_plans": 0,
            "db_size_kb": 0,
        }
    plan = queries.get_plan_by_id(conn, plan_id) if plan_id else queries.get_latest_plan(conn)
    if plan_id and not plan:
        # Asked for a specific plan that doesn't exist
        conn.close()
        return {
            "request": request,
            "error": f"Plan not found: {plan_id}",
            "plan": None,
            "steps": [],
            "skills": [],
            "total_plans": queries.count_total_plans(queries.open_db_readonly()),
            "db_size_kb": queries.get_db_size_kb(),
        }
    steps = queries.get_steps_for_plan(conn, plan["plan_id"]) if plan else []
    # Convert flat list → tree (parent→children) with parallel-group annotations.
    # The template's render_siblings macro expects this shape; passing the flat
    # list still works (children=[] fallback) but you lose the parallel badges.
    steps_tree = queries.build_step_tree(steps) if steps else []
    skills = queries.get_skills_for_plan(conn, plan["plan_id"]) if plan else []
    completed = queries.count_completed_steps(steps)
    total_plans = queries.count_total_plans(conn)
    db_size_kb = queries.get_db_size_kb()
    conn.close()
    return {
        "request": request,
        "error": None,
        "plan": plan,
        "steps": steps_tree,
        "steps_flat": steps,   # kept for any downstream code that expects flat
        "skills": skills,
        "completed": completed,
        "total_steps": len(steps),
        "progress_pct": int(100 * completed / len(steps)) if steps else 0,
        "total_plans": total_plans,
        "db_size_kb": db_size_kb,
        "viewing_plan_id": plan_id,  # None = viewing latest; set = viewing a specific historical plan
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    """Full dashboard page — latest plan. HTMX inside auto-refreshes the partial."""
    context = _build_context(request)
    return TEMPLATES.TemplateResponse(request, "dashboard.html", context)


@app.get("/plan/{plan_id}", response_class=HTMLResponse)
def view_plan(request: Request, plan_id: str):
    """View a specific historical plan by id (linked from /history)."""
    context = _build_context(request, plan_id=plan_id)
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
    })


@app.get("/api/dashboard", response_class=HTMLResponse)
def dashboard_partial(request: Request, plan: str | None = None):
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
        etag = queries.compute_etag(conn)
        conn.close()
    except FileNotFoundError:
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
    context = _build_context(request, plan_id=plan)
    response = TEMPLATES.TemplateResponse(request, "_dashboard_partial.html", context)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/api/health")
def health():
    """JSON ping. Useful for `curl` smoke tests + monitoring."""
    try:
        conn = queries.open_db_readonly()
        plan = queries.get_latest_plan(conn)
        conn.close()
        return JSONResponse({
            "ok": True,
            "latest_plan_id": plan["plan_id"] if plan else None,
            "latest_status": plan["status"] if plan else None,
        })
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "orchestrator.db not found"}, status_code=503)
