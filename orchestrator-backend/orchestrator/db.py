"""SQLite database access layer for the orchestration harness.

Thin CRUD wrappers + migration runner. No ORM. Stdlib sqlite3 only.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Migrations ship INSIDE the package (orchestrator/migrations/) so a pip
# wheel carries them — this resolves in both the repo layout and site-packages.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
DEFAULT_DB_PATH = Path.home() / "skill-workspace" / "orchestrator.db"


def open_db(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open SQLite connection with foreign keys + Row factory enabled. On a DB
    that already carries migration 018, the one-time reclass of the legacy
    reasons / constraints into change_reason runs here (DP phase 1, Task 7);
    a fresh DB gets it from run_migrations' caller on its next open."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _reclass_once(conn)
    return conn


def _reclass_once(conn: sqlite3.Connection) -> None:
    """provenance_migrate.run when 018 is applied and the reclass has not run.
    Never raises and never waits long: the probe uses a 500 ms busy timeout
    (a hook on a locked DB must not stall here), then the connection's
    default timeout is restored; a locked or read-only DB is retried on the
    next open, silently."""
    try:
        conn.execute("PRAGMA busy_timeout=500")
        try:
            have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('migration_state', 'change_reason', 'node_reason')")}
            if len(have) < 3:
                return
            if conn.execute("SELECT 1 FROM migration_state WHERE key='dp_reclass'").fetchone():
                return
            from . import provenance_migrate
            provenance_migrate.run(conn)
        finally:
            conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        pass


def run_migrations(conn: sqlite3.Connection) -> int:
    """Run all migration .sql files in order. Idempotent via schema_version tracking.

    Each migration filename (e.g. '002_step_types_and_agent_io.sql') is recorded
    in schema_version.migration_file after success. Re-runs skip already-applied
    files. Compatible with the original integer-only schema_version table from 001.
    """
    applied = 0
    # FL-021: a DB migrated before filename bookkeeping existed has version rows
    # without migration_file. Reconcile FIRST so only NEW files run below and
    # 001 is never re-executed ("duplicate column name"). Bookkeeping facts:
    # 001 inserts its own marker row (version 1, no filename) and the loop then
    # records one row per applied file as MAX(version)+1 — so a pure legacy DB
    # holds files+1 unlabelled rows, and every DB keeps that one unlabelled
    # marker forever. Rule: labelled rows present -> nothing to reconcile (the
    # marker is 001's); none present -> the unlabelled rows minus the marker are
    # the first N files, in order.
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(schema_version)").fetchall()]
        if cols and "migration_file" not in cols:
            conn.execute("ALTER TABLE schema_version ADD COLUMN migration_file TEXT")
            cols.append("migration_file")
        if cols:
            files = sorted(MIGRATIONS_DIR.glob("*.sql"))
            labelled = [r[0] for r in conn.execute(
                "SELECT migration_file FROM schema_version WHERE migration_file IS NOT NULL")]
            unlabelled = [r[0] for r in conn.execute(
                "SELECT version FROM schema_version WHERE migration_file IS NULL ORDER BY version")]
            if unlabelled and not labelled:
                rows = [v for v in unlabelled if v != 1] if len(unlabelled) > 1 else unlabelled
                for version, f in zip(rows, files):
                    conn.execute("UPDATE schema_version SET migration_file = ? WHERE version = ?", (f.name, version))
                conn.commit()
    except sqlite3.OperationalError:
        pass  # no schema_version yet — a fresh DB; 001 creates it below
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        fname = sql_file.name
        # LAZY bootstrap: ensure schema_version.migration_file column exists
        # BEFORE every iteration. The old code did this once at the top, BEFORE
        # 001 created the table — so on a fresh DB the ALTER failed silently and
        # NO migration ever got recorded with its filename, breaking idempotency
        # forever (second run re-applied everything and exploded on duplicate
        # column). Doing it inside the loop fixes the chicken-and-egg.
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(schema_version)").fetchall()]
            if cols and "migration_file" not in cols:
                conn.execute("ALTER TABLE schema_version ADD COLUMN migration_file TEXT")
        except sqlite3.OperationalError:
            pass  # Table doesn't exist yet — 001 will create it momentarily.

        # Check if this file was already applied (works once migration_file exists)
        try:
            already = conn.execute(
                "SELECT 1 FROM schema_version WHERE migration_file = ?", (fname,)
            ).fetchone()
            if already:
                continue
        except sqlite3.OperationalError:
            pass  # Table doesn't exist yet — will be created by 001
        sql = sql_file.read_text()
        # Phase 4 / 对账自愈: each file runs inside its own SAVEPOINT. A file the
        # bookkeeping lost (one row too few) re-runs and dies on "duplicate
        # column name" / "already exists" — roll it back, record it as applied
        # and carry on. Anything else rolls back and re-raises unchanged.
        try:
            _apply_in_savepoint(conn, sql)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column name" not in msg and "already exists" not in msg:
                raise
            print(f"reconciled {fname}: already applied", file=sys.stderr)
        # Second bootstrap pass: 001 itself CREATES schema_version (without
        # migration_file). If we don't ALTER again here, 001 itself never gets
        # recorded → next run re-applies it. Cheap PRAGMA + idempotent ALTER.
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(schema_version)").fetchall()]
            if cols and "migration_file" not in cols:
                conn.execute("ALTER TABLE schema_version ADD COLUMN migration_file TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            # Use MAX(version)+1 so we never collide with versions inserted
            # by previous runs or by the migration's own SQL.
            next_v = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM schema_version"
            ).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, migration_file) VALUES (?, ?)",
                (next_v, fname),
            )
        except sqlite3.OperationalError:
            pass
        applied += 1
    conn.commit()
    return applied


_SAVEPOINT = "provledger_migration"


def _split_statements(sql: str) -> list[str]:
    """Split a script into complete statements (sqlite3.complete_statement keeps
    trigger bodies whole); comment-only tails are dropped."""
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    if buf.strip() and sqlite3.complete_statement(buf + ";"):
        out.append(buf)
    return out


def _is_pragma(stmt: str) -> bool:
    body = [ln for ln in stmt.splitlines() if ln.strip() and not ln.lstrip().startswith("--")]
    return bool(body) and body[0].lstrip().upper().startswith("PRAGMA")


def _apply_in_savepoint(conn: sqlite3.Connection, sql: str) -> None:
    """Run one migration script with its body inside a SAVEPOINT so a failure
    leaves nothing of it behind. executescript() cannot be used: it COMMITs
    first, which would release the savepoint. A file's leading / trailing PRAGMA
    run (007 toggles foreign_keys + legacy_alter_table around a table rebuild)
    executes OUTSIDE the savepoint, after a commit, exactly as executescript did
    — PRAGMA foreign_keys is a silent no-op inside a transaction."""
    stmts = _split_statements(sql)
    head = 0
    while head < len(stmts) and _is_pragma(stmts[head]):
        head += 1
    tail = len(stmts)
    while tail > head and _is_pragma(stmts[tail - 1]):
        tail -= 1
    conn.commit()                      # PRAGMAs only take effect outside a transaction
    for st in stmts[:head]:
        conn.execute(st)
    conn.execute(f"SAVEPOINT {_SAVEPOINT}")
    try:
        for st in stmts[head:tail]:
            conn.execute(st)
    except Exception:
        conn.execute(f"ROLLBACK TO {_SAVEPOINT}")
        conn.execute(f"RELEASE {_SAVEPOINT}")
        for st in stmts[tail:]:
            conn.execute(st)           # restore the connection state (foreign_keys back ON)
        raise
    conn.execute(f"RELEASE {_SAVEPOINT}")
    for st in stmts[tail:]:
        conn.execute(st)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Group multiple `commit=False` writes into one atomic unit (BE-C4).

    Commits once on success, rolls back on any exception, so a compound operation
    (e.g. insert N sub-steps + bump revision + record deviation) can never leave a
    plan half-mutated. Helpers called inside MUST be passed ``commit=False``.
    """
    with conn:  # sqlite3 connection CM: commit on success, rollback on exception
        yield conn


# ── Plan CRUD ─────────────────────────────────────────────────────────────────
# FL-014: how a plan's project attribution was decided. 'declared' = in the
# plan input (incl. the explicit "none"), 'cwd' = derived from the repo the
# plan was published from, 'legacy' = inferred once from goal text for plans
# that predate the column. Assigned exactly once.
PROJECT_SOURCES = ("declared", "cwd", "legacy")


def insert_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    original_goal: str,
    max_revisions: int = 5,
    status: str = "IN_PROGRESS",
    created_at: str | None = None,
    user_query: str | None = None,
    project: str | None = None,
    project_source: str | None = None,
) -> None:
    if (project is None) != (project_source is None):
        raise ValueError("project and project_source must be given together")
    if project_source is not None and project_source not in PROJECT_SOURCES:
        raise ValueError(f"project_source must be one of {PROJECT_SOURCES}, got {project_source!r}")
    conn.execute(
        "INSERT INTO Plans (plan_id, original_goal, max_revisions, status, created_at, user_query, "
        "project, project_source) VALUES (?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?)",
        (plan_id, original_goal, max_revisions, status, created_at, user_query, project, project_source),
    )
    conn.commit()


def set_plan_project(conn: sqlite3.Connection, plan_id: str, project: str, project_source: str,
                     commit: bool = True) -> None:
    """Assign a plan's project ONCE. A plan that already has one keeps it —
    attribution is a fact recorded at publish (or a one-time legacy inference),
    never re-decided."""
    if project_source not in PROJECT_SOURCES:
        raise ValueError(f"project_source must be one of {PROJECT_SOURCES}, got {project_source!r}")
    if not project:
        raise ValueError("project must be non-empty ('none' means: explicitly no project)")
    cur = conn.execute(
        "UPDATE Plans SET project = ?, project_source = ?, updated_at = ? "
        "WHERE plan_id = ? AND project IS NULL",
        (project, project_source, _now(), plan_id))
    if cur.rowcount != 1:
        current = get_plan(conn, plan_id)
        if current is None:
            raise ValueError(f"plan_id not found: {plan_id}")
        raise ValueError(f"plan {plan_id} is already attributed to {current['project']!r} "
                         f"({current['project_source']}); attribution is assigned once")
    if commit:
        conn.commit()


def get_plan(conn: sqlite3.Connection, plan_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    return dict(row) if row else None


def list_plans(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM Plans ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def set_plan_impact_context(conn: sqlite3.Connection, plan_id: str, impact_context: str) -> None:
    """Persist the Phase D forward-impact analysis JSON on the Plan row."""
    conn.execute(
        "UPDATE Plans SET impact_context = ? WHERE plan_id = ?",
        (impact_context, plan_id),
    )
    conn.commit()


def update_plan_status(conn: sqlite3.Connection, plan_id: str, new_status: str,
                       commit: bool = True) -> None:
    completed_at = _now() if new_status in ("COMPLETED", "FAILED") else None
    conn.execute(
        "UPDATE Plans SET status = ?, updated_at = ?, "
        "completed_at = COALESCE(?, completed_at) WHERE plan_id = ?",
        (new_status, _now(), completed_at, plan_id),
    )
    if commit:
        conn.commit()


def set_review_state(conn: sqlite3.Connection, plan_id: str, review_state: str | None,
                     commit: bool = True) -> None:
    """Set a plan's park-for-review state (BE-D4): 'awaiting_agent' | 'reviewed' | None.

    Lets the dashboard answer "which plans are blocked on agent review?" with a
    single indexed column instead of joining to Steps.
    """
    conn.execute(
        "UPDATE Plans SET review_state = ?, updated_at = ? WHERE plan_id = ?",
        (review_state, _now(), plan_id),
    )
    if commit:
        conn.commit()


def increment_revision(conn: sqlite3.Connection, plan_id: str, commit: bool = True) -> int:
    conn.execute("UPDATE Plans SET revision_count = revision_count + 1 WHERE plan_id = ?", (plan_id,))
    if commit:
        conn.commit()
    row = conn.execute("SELECT revision_count FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    return row["revision_count"] if row else 0


def insert_deviation(
    conn: sqlite3.Connection,
    plan_id: str,
    target_step_id: str | None,
    justification: str,
    new_step_ids: list[str] | None = None,
    revision_count: int | None = None,
    commit: bool = True,
) -> int:
    """Record a deviation in the Deviations history table. Returns deviation_id.

    This is the durable "why a plan changed" audit trail (migration 010).
    """
    cur = conn.execute(
        """INSERT INTO Deviations
           (plan_id, target_step_id, justification, new_step_ids, revision_count)
           VALUES (?, ?, ?, ?, ?)""",
        (plan_id, target_step_id, justification,
         json.dumps(new_step_ids or []), revision_count),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def get_deviations(conn: sqlite3.Connection, plan_id: str) -> list[dict]:
    """All deviations for a plan, oldest first."""
    rows = conn.execute(
        "SELECT * FROM Deviations WHERE plan_id = ? ORDER BY deviation_id", (plan_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Data profile (migration 012) ────────────────────────────────────────────────
def insert_data_profile(conn: sqlite3.Connection, rows: list[dict], commit: bool = True) -> int:
    """Write runtime profile rows (from orchestrator.profiler). Returns row count."""
    for r in rows:
        conn.execute(
            """INSERT INTO data_profile
               (project, plan_id, step_id, dataset, column_name, dtype,
                null_frac, row_count, distinct_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r.get("project"), r.get("plan_id"), r.get("step_id"), r["dataset"],
             r["column_name"], r.get("dtype"), r.get("null_frac"),
             r.get("row_count"), r.get("distinct_count")),
        )
    if commit:
        conn.commit()
    return len(rows)


def get_data_profile(conn: sqlite3.Connection, dataset: str,
                     project: str | None = None) -> list[dict]:
    """Profile rows for a dataset (optionally scoped to a project), oldest first."""
    if project is None:
        rows = conn.execute(
            "SELECT * FROM data_profile WHERE dataset = ? ORDER BY id", (dataset,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM data_profile WHERE dataset = ? AND project = ? ORDER BY id",
            (dataset, project),
        ).fetchall()
    return [dict(r) for r in rows]


# ── LLM decisions (migration 013) ───────────────────────────────────────────────
def insert_llm_decision(
    conn: sqlite3.Connection, *, decision: str,
    project: str | None = None, plan_id: str | None = None, step_id: str | None = None,
    dataset: str | None = None, column: str | None = None,
    observed_before: str | None = None, observed_after: str | None = None,
    drift_kind: str | None = None, rationale: str | None = None,
    action: str | None = None, outcome: str | None = None,
    human_feedback: str | None = None, failure: bool = False,
    sync_ledger: bool = True, commit: bool = True,
) -> int:
    """Record an LLM data decision and (by default) auto-sync it to the ledger.

    The ledger sync writes directly to LedgerEntries in this same DB (no import of
    the writing-plans skill, to avoid a backend→skill dependency). A `failure`
    decision becomes a ledger `anti_pattern`; otherwise a `decision` — so the next
    plan's fuzzy match surfaces it ("never repeat a mistake").
    """
    cur = conn.execute(
        """INSERT INTO llm_decisions
           (project, plan_id, step_id, dataset, column_name, observed_before,
            observed_after, drift_kind, decision, rationale, action, outcome,
            human_feedback)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (project, plan_id, step_id, dataset, column, observed_before,
         observed_after, drift_kind, decision, rationale, action, outcome,
         human_feedback),
    )
    decision_id = int(cur.lastrowid)

    if sync_ledger:
        subjects = json.dumps([s for s in (dataset, column) if s])
        keywords = json.dumps([drift_kind] if drift_kind else [])
        conn.execute(
            """INSERT INTO LedgerEntries
               (project, kind, subjects, keywords, statement, rationale, source, plan_id)
               VALUES (?, ?, ?, ?, ?, ?, 'llm_decision', ?)""",
            (project or "", "anti_pattern" if failure else "decision",
             subjects, keywords, decision, rationale, plan_id),
        )

    if commit:
        conn.commit()
    return decision_id


def get_llm_decisions(conn: sqlite3.Connection, dataset: str | None = None,
                      project: str | None = None) -> list[dict]:
    """LLM decisions, optionally scoped to a dataset and/or project, oldest first."""
    clauses, params = [], []
    if dataset is not None:
        clauses.append("dataset = ?")
        params.append(dataset)
    if project is not None:
        clauses.append("project = ?")
        params.append(project)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM llm_decisions{where} ORDER BY id", params
    ).fetchall()
    return [dict(r) for r in rows]


# ── Step CRUD ─────────────────────────────────────────────────────────────────
def insert_step(
    conn: sqlite3.Connection,
    step_id: str,
    plan_id: str,
    description: str,
    execution_order: int,
    parent_step_id: str | None = None,
    depth_level: int = 0,
    status: str = "PENDING",
    started_at: str | None = None,
    completed_at: str | None = None,
    log_context: str = "",
    step_type: str | None = None,
    commit: bool = True,
) -> None:
    if step_type is not None and step_type not in VALID_STEP_TYPES:
        raise ValueError(f"step_type must be one of {VALID_STEP_TYPES}, got {step_type!r}")
    conn.execute(
        """INSERT INTO Steps
           (step_id, plan_id, parent_step_id, description, status, execution_order,
            depth_level, log_context, started_at, completed_at, step_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (step_id, plan_id, parent_step_id, description, status, execution_order,
         depth_level, log_context, started_at, completed_at, step_type),
    )
    if commit:
        conn.commit()


# ── Reasons / expectations / outcomes (migration 014) ─────────────────────────
# The "why" (node_reason) and the "did it work" (expectations → outcomes) that no
# analysis can recover. All three tables are append-only by trigger; text NULL in
# node_reason means explicitly UNSTATED — never fabricated, never blocking.
VALID_REASON_KINDS = ("reason", "rejected_path", "constraint_ref")
VALID_REASON_SOURCES = ("agent", "human", "system")
VALID_REASON_TIERS = ("stated", "asserted", "derived")


def insert_node_reason(
    conn: sqlite3.Connection,
    *,
    node_key: str | None,
    project: str,
    run_id: int | None,
    plan_id: str,
    kind: str,
    text: str | None,
    source: str,
    tier: str,
    step_id: str | None = None,
    commit: bool = True,
) -> int:
    """DEPRECATED (DP phase 1): the writer is provenance.insert_reason into
    change_reason; this stays for tests and the Task 7 migration only.
    Append one reason row keyed by PSG node_key (survives renames). Raises
    ValueError on a bad kind/source/tier before touching the DB; the table's
    CHECK rejects an unanchored row unless kind == 'rejected_path'."""
    if kind not in VALID_REASON_KINDS or source not in VALID_REASON_SOURCES or tier not in VALID_REASON_TIERS:
        raise ValueError(f"node_reason: bad kind/source/tier {kind!r}/{source!r}/{tier!r}")
    cur = conn.execute(
        "INSERT INTO node_reason (node_key, project, run_id, plan_id, step_id, kind, text, source, tier) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (node_key, project, run_id, plan_id, step_id, kind, text, source, tier))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def get_node_reasons(conn: sqlite3.Connection, *, node_key: str | None = None,
                     plan_id: str | None = None) -> list[dict]:
    where, args = [], []
    if node_key is not None:
        where.append("node_key = ?"); args.append(node_key)
    if plan_id is not None:
        where.append("plan_id = ?"); args.append(plan_id)
    sql = "SELECT * FROM node_reason" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, args)]


def set_review_skip_reason(conn: sqlite3.Connection, plan_id: str, reason: str,
                           commit: bool = True) -> None:
    """Record WHY review_and_complete did not review a plan (S1: never silent)."""
    conn.execute("UPDATE Plans SET review_skip_reason = ? WHERE plan_id = ?", (reason, plan_id))
    if commit:
        conn.commit()


def insert_expectation(conn: sqlite3.Connection, *, plan_id: str, step_id: str | None, project: str,
                       target: str, target_kind: str, claim: str, channel: str,
                       commit: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO expectations (plan_id, step_id, project, target, target_kind, claim, channel) "
        "VALUES (?,?,?,?,?,?,?)",
        (plan_id, step_id, project, target, target_kind, claim, channel))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def get_pending_expectations(conn: sqlite3.Connection, project: str, *, exclude_plan_id: str,
                             kind: str) -> list[dict]:
    """Expectations of OTHER plans of `project` still to judge for `kind`.
    survival (phase 5): EVERY node/column expectation — survival is re-judged at
    each close and the caller appends only when the verdict changed.
    observed: judged once — an observed OR none_available outcome settles it."""
    if kind == "survival":
        return [dict(r) for r in conn.execute(
            """SELECT e.* FROM expectations e
               WHERE e.project = ? AND e.plan_id <> ? AND (e.target_kind IN ('node', 'column') OR e.channel = 'graph')
               ORDER BY e.id""", (project, exclude_plan_id))]
    kinds = ("observed", "none_available") if kind == "observed" else (kind, kind)
    return [dict(r) for r in conn.execute(
        """SELECT e.* FROM expectations e
           WHERE e.project = ? AND e.plan_id <> ?
             AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.expectation_id = e.id AND o.kind IN (?, ?))
           ORDER BY e.id""", (project, exclude_plan_id, *kinds))]


def insert_outcome(conn: sqlite3.Connection, *, expectation_id: int, kind: str, value,
                   source: str, tier: str, reason: str | None = None,
                   backfilled_by_plan: str | None = None, commit: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO outcomes (expectation_id, kind, value_json, source, tier, reason, backfilled_by_plan) "
        "VALUES (?,?,?,?,?,?,?)",
        (expectation_id, kind, json.dumps(value, sort_keys=True, default=str), source, tier, reason,
         backfilled_by_plan))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def insert_metric(conn: sqlite3.Connection, *, project: str, name: str, value, unit: str | None = None,
                  plan_id: str | None = None, step_id: str | None = None, source: str = "record-metric",
                  observed_at: str | None = None, commit: bool = True) -> int:
    """One numeric observation (migration 017). value must be a finite real
    number — never a string, bool or NaN: metrics are measured, not written.
    observed_at (optional 'YYYY-MM-DD HH:MM:SS') is for observations made
    earlier than they are recorded; the table is append-only, so it cannot be
    fixed afterwards."""
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"metric value must be a number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ValueError(f"metric value must be finite, got {value!r}")
    if not project or not name:
        raise ValueError("project and name are required")
    if observed_at is None:
        cur = conn.execute(
            "INSERT INTO metrics (project, plan_id, step_id, name, value, unit, source) VALUES (?,?,?,?,?,?,?)",
            (project, plan_id, step_id, name, float(value), unit, source))
    else:
        cur = conn.execute(
            "INSERT INTO metrics (project, plan_id, step_id, name, value, unit, source, observed_at) VALUES (?,?,?,?,?,?,?,?)",
            (project, plan_id, step_id, name, float(value), unit, source, observed_at))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def get_metrics(conn: sqlite3.Connection, project: str, name: str, *, before: str | None = None,
                after: str | None = None) -> list[dict]:
    """Metric rows of (project, name) oldest first; `before` keeps rows observed
    at or before that timestamp, `after` rows observed strictly after it."""
    sql = "SELECT * FROM metrics WHERE project = ? AND name = ?"
    args: list = [project, name]
    if before is not None:
        sql += " AND observed_at <= ?"; args.append(before)
    if after is not None:
        sql += " AND observed_at > ?"; args.append(after)
    sql += " ORDER BY observed_at, id"
    return [dict(r) for r in conn.execute(sql, args)]


def get_outcomes(conn: sqlite3.Connection, expectation_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM outcomes WHERE expectation_id = ? ORDER BY id", (expectation_id,))]


def get_step(conn: sqlite3.Connection, step_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM Steps WHERE step_id = ?", (step_id,)).fetchone()
    return dict(row) if row else None


def get_steps(conn: sqlite3.Connection, plan_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM Steps WHERE plan_id = ? ORDER BY execution_order, step_id",
        (plan_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_children(conn: sqlite3.Connection, parent_step_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM Steps WHERE parent_step_id = ? ORDER BY execution_order",
        (parent_step_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_step_status(
    conn: sqlite3.Connection,
    step_id: str,
    new_status: str,
    set_started: bool = False,
    set_completed: bool = False,
    commit: bool = True,
) -> None:
    sets = ["status = ?", "updated_at = ?"]
    params: list = [new_status, _now()]
    if set_started:
        sets.append("started_at = ?")
        params.append(_now())
    if set_completed:
        # COALESCE: never re-stamp an already-set completed_at (BE-C5) — the first
        # terminal time is the truth, re-completion must not overwrite it.
        sets.append("completed_at = COALESCE(completed_at, ?)")
        params.append(_now())
    params.append(step_id)
    conn.execute(f"UPDATE Steps SET {', '.join(sets)} WHERE step_id = ?", params)
    if commit:
        conn.commit()


def set_failure_reason(conn: sqlite3.Connection, step_id: str, reason: str,
                       commit: bool = True) -> None:
    """Persist a step's failure reason as a first-class column (BE-D3).

    Survives log_context truncation; the dashboard reads this directly.
    """
    conn.execute(
        "UPDATE Steps SET failure_reason = ?, updated_at = ? WHERE step_id = ?",
        (reason, _now(), step_id),
    )
    if commit:
        conn.commit()


def update_step_log(conn: sqlite3.Connection, step_id: str, log_context: str) -> None:
    conn.execute("UPDATE Steps SET log_context = ? WHERE step_id = ?", (log_context, step_id))
    conn.commit()


# ── v4: Auto-review-and-complete marker step (migration 006) ────────────────────────
REVIEW_STEP_DESCRIPTION = (
    "AUTO: review all prior steps and finalize plan status (deterministic, no LLM)"
)


def insert_review_step(conn: sqlite3.Connection, plan_id: str) -> str:
    """Append the canonical auto-review-and-complete marker step to a plan.

    Used by writing-plans/publish-plan to ensure every newly-published plan has
    a terminal step that the deterministic api.review_and_complete procedure can
    flip when all sibling (non-review) steps have reached a terminal state.

    Idempotent? NO. Calling twice on the same plan raises an IntegrityError on
    the PRIMARY KEY conflict for step_id = '<plan_id>-REVIEW'. The publish-plan
    helper must call this exactly once per plan.

    Returns the new step_id.
    """
    step_id = f"{plan_id}-REVIEW"
    max_order_row = conn.execute(
        "SELECT COALESCE(MAX(execution_order), -1) AS m FROM Steps WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    next_order = (max_order_row["m"] + 1) if max_order_row else 0
    conn.execute(
        """INSERT INTO Steps
           (step_id, plan_id, parent_step_id, description, status, execution_order,
            depth_level, log_context, started_at, completed_at, step_type, is_review)
           VALUES (?, ?, NULL, ?, 'PENDING', ?, 0, '', NULL, NULL, NULL, 1)""",
        (step_id, plan_id, REVIEW_STEP_DESCRIPTION, next_order),
    )
    conn.commit()
    return step_id


# ── v2: step_type + agent I/O ────────────────────────────────────────
VALID_STEP_TYPES = {"THINKING", "DOCUMENTATION", "CODE", "COMMAND", "SUB_AGENT", "ANALYSIS"}


def set_step_type(conn: sqlite3.Connection, step_id: str, step_type: str) -> None:
    """Set or change the type of a step. Validates against allowed values."""
    if step_type not in VALID_STEP_TYPES:
        raise ValueError(f"step_type must be one of {VALID_STEP_TYPES}, got {step_type!r}")
    conn.execute("UPDATE Steps SET step_type = ? WHERE step_id = ?", (step_type, step_id))
    conn.commit()


def set_agent_input(conn: sqlite3.Connection, step_id: str, agent_input: str) -> None:
    """Record the input prompt sent to a sub-agent. Should pair with step_type='SUB_AGENT'."""
    conn.execute("UPDATE Steps SET agent_input = ? WHERE step_id = ?", (agent_input, step_id))
    conn.commit()


def set_agent_output(conn: sqlite3.Connection, step_id: str, agent_output: str) -> None:
    """Record the output returned from a sub-agent. Should pair with step_type='SUB_AGENT'."""
    conn.execute("UPDATE Steps SET agent_output = ? WHERE step_id = ?", (agent_output, step_id))
    conn.commit()


def set_step_summary(conn: sqlite3.Connection, step_id: str, summary: str) -> None:
    """Set the human-curated 1-line summary for a step.

    Distinct from log_context (raw machine output). The summary is what
    a reviewer skimming the dashboard sees first; the log is the messy
    detail they expand into when they want the full story.
    """
    conn.execute("UPDATE Steps SET summary = ? WHERE step_id = ?", (summary, step_id))
    conn.commit()


# ── v3: Skill Activations ─────────────────────────────────────────────────
VALID_SKILL_SOURCES = {
    "auto-search",       # proactive skill search before any action
    "explicit-mention",  # user mentioned skill name in their prompt
    "iron-law",          # mandatory trigger (TDD, writing-plans)
    "deferred-load",     # loaded later as task evolved
}


def add_skill_activation(
    conn: sqlite3.Connection,
    plan_id: str,
    skill_name: str,
    source: str,
    step_id: str | None = None,
    reason: str | None = None,
) -> int:
    """Record one skill activation event. Returns the new activation_id.

    `source` must be one of VALID_SKILL_SOURCES (Python-side check provides a
    nicer ValueError; the SQL CHECK constraint is the durable enforcement).
    `step_id` is optional — NULL means "activated at plan-init time".
    """
    if source not in VALID_SKILL_SOURCES:
        raise ValueError(
            f"source must be one of {sorted(VALID_SKILL_SOURCES)}, got {source!r}"
        )
    cur = conn.execute(
        """INSERT INTO SkillActivations
           (plan_id, step_id, skill_name, source, reason)
           VALUES (?, ?, ?, ?, ?)""",
        (plan_id, step_id, skill_name, source, reason),
    )
    conn.commit()
    return cur.lastrowid


def get_skill_activations(
    conn: sqlite3.Connection,
    plan_id: str,
    step_id: str | None = None,
) -> list[dict]:
    """Return activations for a plan, optionally filtered to one step.

    Ordered by activated_at ASC (insertion order — oldest first), then by
    activation_id ASC as a stable tiebreaker for same-timestamp rows.
    """
    if step_id is not None:
        rows = conn.execute(
            """SELECT * FROM SkillActivations
               WHERE plan_id = ? AND step_id = ?
               ORDER BY activated_at ASC, activation_id ASC""",
            (plan_id, step_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM SkillActivations
               WHERE plan_id = ?
               ORDER BY activated_at ASC, activation_id ASC""",
            (plan_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_skill_uses_for_plan(
    conn: sqlite3.Connection,
    plan_id: str,
) -> dict[str, int]:
    """Return {skill_name: count} for one plan. Useful for per-plan analytics."""
    rows = conn.execute(
        """SELECT skill_name, COUNT(*) AS n FROM SkillActivations
           WHERE plan_id = ? GROUP BY skill_name""",
        (plan_id,),
    ).fetchall()
    return {r["skill_name"]: r["n"] for r in rows}
