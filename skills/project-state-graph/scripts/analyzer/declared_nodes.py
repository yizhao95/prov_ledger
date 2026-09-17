"""The declared stage (DP phase 2c, spec §18): put what the user declared into
the graph the analyzer just built.

Every other stage reads source. This one reads the ORCHESTRATOR database — the
`declared_node` table, where `provledger node declare` records business rules,
external systems, stakeholder decisions, external datasets and hand-computed
figures — and writes one graph node per ACTIVE declaration plus its
`declared_feeds` / `declared_constrains` / `declared_depends_on` edges to the
code nodes it names.

It writes nodes and edges, nothing else. The identity, the history and the
events are the host's, through `provledger.declared` (the provider) and the
history layer, exactly as for a function. That is the whole point of §18: a
declared node is not an exception, it is a node whose data happens to be
`stated` or `asserted` instead of `observed`.

A project nobody has declared anything for gets nothing — which is why adding
this stage changed no scenario golden.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

from . import store
from ._host import db as _db
from ._host import declared as _declared

DECLARED_TYPES = ("external_system", "business_rule", "stakeholder_decision", "external_dataset", "manual_figure")
LINK_KINDS = ("declared_feeds", "declared_constrains", "declared_depends_on")


def orchestrator_db_path() -> str:
    return os.environ.get("ORCH_DB") or str(_db.DEFAULT_DB_PATH)


def _active(project: str, path: str) -> list[dict]:
    """The project's active declarations, read-only. A database that is not
    there yet is not a failure (nobody has declared anything); one that is
    there and unreadable is said out loud."""
    if not os.path.exists(path):
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        print(f"WARNING: declared stage could not open {path}: {e}", file=sys.stderr)
        return []
    try:
        return _declared.active(conn, project)
    except sqlite3.Error as e:      # an older orchestrator DB without migration 023
        print(f"WARNING: declared stage found no declared_node table in {path} ({e})", file=sys.stderr)
        return []
    finally:
        conn.close()


def _resolve(conn, qualified_name: str) -> int | None:
    """The node this link names, when exactly one node carries that name.
    Two matches resolve to nothing: guessing which one a declaration meant is
    worse than recording that the link did not land."""
    rows = conn.execute("SELECT id FROM node WHERE qualified_name = ? AND run_id IS NULL", (qualified_name,)).fetchall()
    return int(rows[0][0]) if len(rows) == 1 else None


def analyze(conn, repo_root: str, project: str, orch_db: str | None = None) -> dict:
    """-> {declarations, edges, unresolved}. `repo_root` is unused: a declared
    node has no file — it is the part of the project that is not in the repo."""
    rows = _active(project, orch_db or orchestrator_db_path())
    made = links = unresolved = 0
    for row in rows:
        node_type = row["node_type"]
        if node_type not in DECLARED_TYPES:
            print(f"WARNING: declared stage skipped {row['qualified_name']}: unknown type {node_type!r}", file=sys.stderr)
            continue
        try:
            attrs = json.loads(row["attrs_json"] or "{}")
            link_rows = json.loads(row["links_json"] or "[]")
            field_tiers = json.loads(row["field_tiers_json"] or "{}")
        except ValueError:
            attrs, link_rows, field_tiers = {}, [], {}
        metadata = {"declared_id": row["id"], "slug": row["slug"], "node_type": node_type,
                    "state": row["state"], "tier": row["tier"], "version": row["version"],
                    "description": row["description"], "attrs": attrs, "links": link_rows,
                    "links_checked": row["links_checked"], "field_tiers": field_tiers,
                    "description_utterance_id": row["description_utterance_id"]}
        type_id = store.get_or_create_node_type(conn, node_type)
        nid = store.add_node(conn, type_id, name=row["slug"], qualified_name=row["qualified_name"],
                             file_path=None, line_start=None, metadata=metadata)
        made += 1
        for link in link_rows:
            kind = link.get("kind") if isinstance(link, dict) else None
            target = link.get("to") if isinstance(link, dict) else None
            if kind not in LINK_KINDS or not target:
                continue
            dst = _resolve(conn, target)
            if dst is None:
                unresolved += 1
                continue
            store.add_edge(conn, store.get_or_create_edge_type(conn, kind), nid, dst,
                           metadata={"declared_by": link.get("by") or "user", "declared_id": row["id"]})
            links += 1
    conn.commit()
    return {"declarations": made, "edges": links, "unresolved": unresolved}
