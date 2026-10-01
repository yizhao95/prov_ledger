#!/usr/bin/env python3
"""ledger_store.py — provLedger Phase E manual decision-memory store.

The "ledger track": stores DECISIONS (+ rationale), ANTI-PATTERNS (failures +
cause) and CONSTRAINTS (externally imposed rules anchored to data points) scoped
to a project, with subject symbols/tables/node_keys + free keywords used for
plan-time matching. Populated by MANUAL entries (ledger_cli.py) — gradual,
opt-in, never auto-populated.

Constraints (spec §2.9): `subjects` may carry PSG node_keys (nk_…) or
owner.column names; constraints_for matches them EXACTLY, never lexically.
`why_ref` points at the source of the WHY (meeting notes, decision doc);
`why_visibility='restricted'` keeps the rationale inside the ledger — only the
reference leaves. No category taxonomy lives here (E4-4): anything beyond
`kind` is a free keyword.

Stdlib only (sqlite3 + json). Rows are returned as plain dicts with `subjects`
and `keywords` already decoded from JSON into lists.

The LedgerEntries table is created by orchestrator migration 009; this module
only reads/writes it.
"""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

VALID_KINDS = ("decision", "anti_pattern", "constraint")
VALID_VISIBILITY = ("shared", "restricted")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["subjects"] = json.loads(d["subjects"]) if d.get("subjects") else []
    d["keywords"] = json.loads(d["keywords"]) if d.get("keywords") else []
    return d


def add_entry(conn: sqlite3.Connection, *, project: str, kind: str,
              statement: str, rationale: str = "",
              subjects: Optional[List[str]] = None,
              keywords: Optional[List[str]] = None,
              source: str = "manual",
              plan_id: Optional[str] = None,
              why_ref: Optional[str] = None,
              why_visibility: str = "shared") -> int:
    """Insert one ledger entry. Returns the new row id. Raises ValueError on a
    bad kind / visibility (caught before hitting the DB so callers get a clean
    message).

    `plan_id` (SK-D1) records the provenance plan that produced this decision.
    `why_ref` / `why_visibility` (migration 015) anchor the WHY to an external
    reference and say whether the rationale may leave the ledger.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind {kind!r}; must be one of {VALID_KINDS}")
    if why_visibility not in VALID_VISIBILITY:
        raise ValueError(f"invalid why_visibility {why_visibility!r}; must be one of {VALID_VISIBILITY}")
    cur = conn.execute(
        "INSERT INTO LedgerEntries "
        "(project, kind, subjects, keywords, statement, rationale, source, plan_id, why_ref, why_visibility) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project, kind, json.dumps(subjects or []), json.dumps(keywords or []),
         statement, rationale, source, plan_id, why_ref, why_visibility))
    conn.commit()
    # All three kinds are mirrored into change_reason, because that is where the
    # readers moved in DP phase 1 and the one-time backfill is long since done.
    # Only `constraint` was wired, so every `decision` and `anti_pattern` added
    # afterwards landed where no answer path looks — and those two are the whole
    # reason this ledger exists. Each is mirrored as what it IS, never as a
    # constraint: an anti-pattern is a path that was tried and failed, which is
    # exactly `rejected_path`; a decision is a `reason`.
    if kind == "constraint":
        _mirror_constraint(conn, project=project, subjects=subjects or [], statement=statement, rationale=rationale,
                           plan_id=plan_id, why_ref=why_ref, why_visibility=why_visibility)
    else:
        _mirror_as_reason(conn, kind=kind, project=project, subjects=subjects or [], statement=statement,
                          rationale=rationale, plan_id=plan_id, why_ref=why_ref, why_visibility=why_visibility)
    return int(cur.lastrowid)


def _mirror_constraint(conn, *, project, subjects, statement, rationale, plan_id, why_ref, why_visibility) -> None:
    """DP phase 1 (Task 7): the readers moved to change_reason, so a new ledger
    constraint is written there too (orchestrator.constraints.record_constraint);
    LedgerEntries keeps its row until the next phase stops writing it."""
    try:
        from orchestrator import constraints as _constraints
    except ImportError:
        return
    try:
        _constraints.record_constraint(conn, project=project, subjects=subjects, statement=statement, rationale=rationale,
                                       plan_id=plan_id, why_ref=why_ref, why_visibility=why_visibility)
    except Exception as exc:          # a DB without 018: the ledger row still exists
        import sys
        print(f"warning: constraint not mirrored into change_reason: {exc}", file=sys.stderr)


_MIRROR_ROLE = {"anti_pattern": "rejected_path", "decision": "reason"}


def _mirror_as_reason(conn, *, kind, project, subjects, statement, rationale, plan_id,
                      why_ref, why_visibility) -> None:
    """A `decision` or an `anti_pattern` as change_reason rows — one per subject,
    under the role that says what it is.

    The role matters more than it looks: a reader deciding whether to repeat
    something needs "this was tried and it failed" to arrive as a rejected path,
    not as a constraint it must satisfy nor as a plain reason for the current
    shape. `tier` is `asserted` and `recorded_by` is `human`, the same as the
    constraint mirror — a person typed this at a terminal, and it is their reading
    of what happened rather than a quote of anyone's words."""
    role = _MIRROR_ROLE.get(kind)
    if role is None:
        return
    subjects = [s for s in (subjects or []) if s]
    if not subjects:
        return
    try:
        from orchestrator import provenance as _prov
    except ImportError:
        return
    try:
        refs = []
        if why_ref:
            refs.append(_prov.insert_reference(
                conn, project=project, kind="doc", label=str(why_ref)[:512],
                occurred_at=conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S','now')").fetchone()[0],
                commit=False))
        for subject in subjects:
            _prov.insert_reason(
                conn, project=project, plan_id=plan_id or "ledger", node_key=subject,
                kind="organizational", role=role, statement=statement,
                rationale=rationale or None,
                rationale_visibility="personal" if why_visibility == "restricted" else "shareable",
                refs=refs, recorded_by="human", state="active", commit=False)
        conn.commit()
    except Exception as exc:          # a DB without 018: the ledger row still exists
        import sys
        print(f"warning: {kind} not mirrored into change_reason: {exc}", file=sys.stderr)


def constraints_for(conn: sqlite3.Connection, project: str,
                    node_keys: List[str], qualified_names: Optional[List[str]] = None) -> List[dict]:
    """Active constraint entries whose subjects contain ANY of `node_keys` or
    `qualified_names` — exact match on the anchor (a node_key, a qualified name
    or an owner.column), never lexical. A restricted entry comes back with
    rationale=None: only why_ref leaves the ledger. (The ledger row; its
    change_reason twin is what orchestrator.constraints reads at close time.)"""
    keys = [k for k in (*(node_keys or []), *(qualified_names or [])) if k]
    if not keys:
        return []
    rows = conn.execute(
        "SELECT DISTINCT l.* FROM LedgerEntries l, json_each(l.subjects) s "
        "WHERE l.project = ? AND l.kind = 'constraint' AND l.status = 'active' "
        f"AND s.value IN ({','.join('?' * len(keys))}) ORDER BY l.id",
        (project, *keys)).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        if d.get("why_visibility") == "restricted":
            d["rationale"] = None
        out.append(d)
    return out


def get_entries(conn: sqlite3.Connection, project: str, *,
                include_superseded: bool = False) -> List[dict]:
    """All entries for a project (active only unless include_superseded)."""
    if include_superseded:
        rows = conn.execute(
            "SELECT * FROM LedgerEntries WHERE project = ? "
            "ORDER BY created_at, id", (project,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM LedgerEntries WHERE project = ? AND status = 'active' "
            "ORDER BY created_at, id", (project,)).fetchall()
    return [_row_to_dict(r) for r in rows]


# query_entries is an alias kept for callers that prefer the verb 'query'.
query_entries = get_entries


def supersede_entry(conn: sqlite3.Connection, entry_id: int,
                    superseded_by: Optional[int] = None) -> None:
    """Mark an entry superseded so it stops surfacing as a reminder.

    `superseded_by` (SK-D1) records which entry replaced it, and `updated_at`
    records when — turning supersession into an auditable lineage.
    """
    conn.execute(
        "UPDATE LedgerEntries SET status = 'superseded', superseded_by = ?, "
        "updated_at = strftime('%Y-%m-%d %H:%M:%S','now') WHERE id = ?",
        (superseded_by, entry_id))
    conn.commit()


def record_hit(conn: sqlite3.Connection, entry_id: int, plan_id: Optional[str] = None,
               step_id: Optional[str] = None) -> int:
    """DP phase 2: surfacing a constraint at plan time is a read_hit
    (moment 'plan') on its change_reason twin — the system told the agent;
    nothing says anyone read it. LedgerEntries.hit_count no longer moves. A
    decision has no twin and records nothing. Returns the read_hit rows written."""
    row = conn.execute("SELECT project, kind, statement FROM LedgerEntries WHERE id = ?", (entry_id,)).fetchone()
    if row is None or row[1] != "constraint":
        return 0
    try:
        twin = conn.execute(
            "SELECT id FROM change_reason WHERE project = ? AND role = 'constraint' AND statement = ? "
            "AND state = 'active' AND superseded_by IS NULL ORDER BY id DESC LIMIT 1", (row[0], row[2])).fetchone()
        if twin is None:
            return 0
        conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, step_id, moment) VALUES (?, ?, ?, ?, 'plan')",
                     (twin[0], row[0], plan_id, step_id))
        conn.commit()
        return 1
    except sqlite3.OperationalError:          # a DB without migration 019
        return 0


