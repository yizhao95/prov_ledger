#!/usr/bin/env python3
"""ledger_store.py — provLedger Phase E manual decision-memory store.

The "ledger track": stores DECISIONS (+ rationale) and ANTI-PATTERNS (failures +
cause) scoped to a project, with subject symbols/tables + free keywords used for
plan-time fuzzy matching. Populated by MANUAL entries (ledger_cli.py) — gradual,
opt-in, never auto-populated.

Stdlib only (sqlite3 + json). Rows are returned as plain dicts with `subjects`
and `keywords` already decoded from JSON into lists.

The LedgerEntries table is created by orchestrator migration 009; this module
only reads/writes it.
"""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

VALID_KINDS = ("decision", "anti_pattern")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["subjects"] = json.loads(d["subjects"]) if d.get("subjects") else []
    d["keywords"] = json.loads(d["keywords"]) if d.get("keywords") else []
    return d


def add_entry(conn: sqlite3.Connection, *, project: str, kind: str,
              statement: str, rationale: str = "",
              subjects: Optional[List[str]] = None,
              keywords: Optional[List[str]] = None,
              source: str = "manual") -> int:
    """Insert one ledger entry. Returns the new row id. Raises ValueError on a
    bad kind (caught before hitting the DB so callers get a clean message)."""
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind {kind!r}; must be one of {VALID_KINDS}")
    cur = conn.execute(
        "INSERT INTO LedgerEntries "
        "(project, kind, subjects, keywords, statement, rationale, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project, kind, json.dumps(subjects or []), json.dumps(keywords or []),
         statement, rationale, source))
    conn.commit()
    return int(cur.lastrowid)


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


def supersede_entry(conn: sqlite3.Connection, entry_id: int) -> None:
    """Mark an entry superseded so it stops surfacing as a reminder."""
    conn.execute("UPDATE LedgerEntries SET status = 'superseded' WHERE id = ?",
                 (entry_id,))
    conn.commit()
