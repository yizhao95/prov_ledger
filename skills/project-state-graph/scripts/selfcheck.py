"""Graph self-checks: deterministic invariants on a built state-graph DB.

These are NOT unit tests of the analyzer code; they validate that a *produced*
graph is internally sound before we trust it. Run after every build.

Each check carries a severity:
  - error   : a failure flips the overall result to FAIL (build/review must stop)
  - warning : a failure is surfaced ([WARN]) but NEVER blocks (exit stays 0)

Invariants (error):
  - node_types_nonempty    : at least one node type exists with nodes
  - no_dangling_edges      : every edge src/dst references an existing node
  - cards_match_callables  : consistency_card & symbol_card counts each equal
                             the number of function+method nodes (full coverage)
  - commit_sha_set         : the latest analysis_run recorded a commit_sha
  - no_undefined_symbols   : no unresolved_call nodes (a bare-name call that
                             resolves to nothing — likely a rename/typo). HARD.

Invariants (warning):
  - no_isolated_nodes      : function/method nodes with no behavioral edge
                             (dead code). Yellow warning, non-blocking.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

# Edge types that count as a callable being "connected" behaviorally.
# (`defines` is the file->symbol structural edge and is intentionally excluded.)
_BEHAVIORAL_EDGES = (
    "calls", "produces", "consumes", "pipeline_step", "reads_sql", "writes_sql",
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _node_type_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM node_type WHERE name=?", (name,)
    ).fetchone()
    return row is not None


def _check_node_types_nonempty(conn) -> Dict[str, Any]:
    n = conn.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    types = conn.execute("SELECT COUNT(*) FROM node_type").fetchone()[0]
    ok = n > 0 and types > 0
    return {"name": "node_types_nonempty", "ok": ok, "severity": "error",
            "detail": f"{n} nodes across {types} node types"}


def _check_no_dangling_edges(conn) -> Dict[str, Any]:
    dangling = conn.execute(
        """SELECT COUNT(*) FROM edge e
           WHERE NOT EXISTS (SELECT 1 FROM node n WHERE n.id = e.src_node_id)
              OR NOT EXISTS (SELECT 1 FROM node n WHERE n.id = e.dst_node_id)"""
    ).fetchone()[0]
    return {"name": "no_dangling_edges", "ok": dangling == 0, "severity": "error",
            "detail": f"{dangling} dangling edge(s)"}


def _check_cards_match_callables(conn) -> Dict[str, Any]:
    if not (_table_exists(conn, "consistency_card") and _table_exists(conn, "symbol_card")):
        return {"name": "cards_match_callables", "ok": False, "severity": "error",
                "detail": "card tables missing"}
    callables = conn.execute(
        """SELECT COUNT(*) FROM node n JOIN node_type t ON n.node_type_id = t.id
           WHERE t.name IN ('function', 'method')"""
    ).fetchone()[0]
    cc = conn.execute("SELECT COUNT(*) FROM consistency_card").fetchone()[0]
    sc = conn.execute("SELECT COUNT(*) FROM symbol_card").fetchone()[0]
    ok = callables > 0 and cc == callables and sc == callables
    return {"name": "cards_match_callables", "ok": ok, "severity": "error",
            "detail": f"callables={callables} consistency_card={cc} symbol_card={sc}"}


def _check_commit_sha_set(conn) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT commit_sha FROM analysis_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    ok = row is not None and row[0] not in (None, "")
    return {"name": "commit_sha_set", "ok": ok, "severity": "error",
            "detail": f"latest commit_sha={row[0] if row else 'NO RUN'}"}


def _check_no_undefined_symbols(conn) -> Dict[str, Any]:
    """HARD check: any unresolved_call node is a bare-name call that resolves to
    nothing (project callable / import / builtin / local). Likely a rename/typo."""
    if not _node_type_exists(conn, "unresolved_call"):
        return {"name": "no_undefined_symbols", "ok": True, "severity": "error",
                "detail": "0 undefined symbol(s)"}
    rows = conn.execute(
        """SELECT n.name, n.file_path, n.line_start
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name='unresolved_call'
           ORDER BY n.file_path, n.line_start"""
    ).fetchall()
    if not rows:
        return {"name": "no_undefined_symbols", "ok": True, "severity": "error",
                "detail": "0 undefined symbol(s)"}
    sample = "; ".join(
        f"{name}() @ {fpath}:{line}" for name, fpath, line in rows[:5]
    )
    more = "" if len(rows) <= 5 else f" (+{len(rows) - 5} more)"
    return {"name": "no_undefined_symbols", "ok": False, "severity": "error",
            "detail": f"{len(rows)} undefined symbol(s): {sample}{more}"}


def _check_no_isolated_nodes(conn) -> Dict[str, Any]:
    """WARNING check: function/method nodes with no behavioral edge (dead code)."""
    placeholders = ",".join("?" for _ in _BEHAVIORAL_EDGES)
    rows = conn.execute(
        f"""
        SELECT n.name, n.file_path FROM node n
        JOIN node_type t ON n.node_type_id=t.id
        WHERE t.name IN ('function', 'method')
          AND NOT EXISTS (
            SELECT 1 FROM edge e
            JOIN edge_type et ON e.edge_type_id=et.id
            WHERE et.name IN ({placeholders})
              AND (e.src_node_id=n.id OR e.dst_node_id=n.id)
          )
        ORDER BY n.file_path, n.name
        """,
        _BEHAVIORAL_EDGES,
    ).fetchall()
    if not rows:
        return {"name": "no_isolated_nodes", "ok": True, "severity": "warning",
                "detail": "0 isolated callable(s)"}
    sample = "; ".join(f"{name} ({fpath})" for name, fpath in rows[:5])
    more = "" if len(rows) <= 5 else f" (+{len(rows) - 5} more)"
    return {"name": "no_isolated_nodes", "ok": False, "severity": "warning",
            "detail": f"{len(rows)} isolated callable(s): {sample}{more}"}


def _dtype_is_typed(meta_json) -> bool:
    """Shared rule: a data node is 'typed' iff its metadata carries a known
    dtype (or type) that is not the 'unknown' sentinel. runtime-probe provenance
    still counts (it has produced a concrete dtype)."""
    import json
    info = json.loads(meta_json) if meta_json else {}
    dtype = info.get("dtype", info.get("type", "unknown"))
    return dtype not in (None, "unknown", "")


def dtype_coverage(conn) -> Dict[str, Any]:
    """Headline metric (Phase C-2): how much of the data surface is typed.

    Counts column / data_var nodes; ``typed`` are those with a known dtype,
    ``unknown`` the rest. Returns {typed, unknown, total, pct} where
    pct = round(100*typed/total, 1), and 0.0 for an empty data surface (no
    ZeroDivision). Every data gate is exactly as strong as this number.
    """
    # PSG-D1: set-based count over the indexed `dtype` column (no per-row
    # json.loads). `dtype` mirrors metadata["dtype"]; 'unknown'/''/NULL == untyped.
    total = conn.execute(
        """SELECT COUNT(*) FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('column', 'data_var')"""
    ).fetchone()[0]
    typed = conn.execute(
        """SELECT COUNT(*) FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('column', 'data_var')
             AND n.dtype IS NOT NULL AND n.dtype NOT IN ('unknown', '')"""
    ).fetchone()[0]
    unknown = total - typed
    pct = round(100.0 * typed / total, 1) if total else 0.0
    return {"typed": typed, "unknown": unknown, "total": total, "pct": pct}


def _check_dtype_present(conn) -> Dict[str, Any]:
    """WARNING: column / data_var nodes left with an unknown, unprobed dtype."""
    rows = conn.execute(
        """SELECT n.name, n.file_path, n.metadata_json
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('column', 'data_var')"""
    ).fetchall()
    import json
    bad = []
    for name, fpath, meta in rows:
        info = json.loads(meta) if meta else {}
        dtype = info.get("dtype", info.get("type", "unknown"))
        prov = info.get("dtype_provenance", "unknown")
        if dtype in (None, "unknown") and prov != "runtime-probe":
            bad.append(f"{name} ({fpath})")
    if not bad:
        return {"name": "dtype_present", "ok": True, "severity": "warning",
                "detail": "all data nodes typed"}
    sample = "; ".join(bad[:5])
    more = "" if len(bad) <= 5 else f" (+{len(bad) - 5} more)"
    return {"name": "dtype_present", "ok": False, "severity": "warning",
            "detail": f"{len(bad)} untyped data node(s): {sample}{more}"}


def _check_dtype_consistency_e2e(conn) -> Dict[str, Any]:
    """ERROR: a produced data_var's dtype must agree with what each consumer
    expects. We compare the producer's output type against the CONSUMER's declared
    param type (consumes.metadata.expected_type, PSG-C4) — falling back to the
    legacy `type` field when a consumer's expected type wasn't resolved. A mismatch
    of two known concrete types is an end-to-end dtype break."""
    import json
    produced_type: Dict[int, str] = {}
    for _src, dv, meta in conn.execute(
        """SELECT e.src_node_id, e.dst_node_id, e.metadata_json
           FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           WHERE t.name='produces'"""
    ).fetchall():
        info = json.loads(meta) if meta else {}
        produced_type[int(dv)] = info.get("type", "unknown")
    mismatches = []
    for dv, _cons, meta in conn.execute(
        """SELECT e.src_node_id, e.dst_node_id, e.metadata_json
           FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           WHERE t.name='consumes'"""
    ).fetchall():
        info = json.loads(meta) if meta else {}
        # PSG-C4: prefer the consumer's declared param type; fall back to legacy.
        want = info.get("expected_type") or info.get("type", "unknown")
        have = produced_type.get(int(dv), "unknown")
        if want not in (None, "unknown") and have not in (None, "unknown") \
                and want != have:
            nm = conn.execute("SELECT name FROM node WHERE id=?", (dv,)).fetchone()
            mismatches.append(f"{nm[0] if nm else dv}: produced {have} != consumed {want}")
    if not mismatches:
        return {"name": "dtype_consistency_e2e", "ok": True, "severity": "error",
                "detail": "no end-to-end dtype mismatches"}
    sample = "; ".join(mismatches[:5])
    more = "" if len(mismatches) <= 5 else f" (+{len(mismatches) - 5} more)"
    return {"name": "dtype_consistency_e2e", "ok": False, "severity": "error",
            "detail": f"{len(mismatches)} dtype break(s): {sample}{more}"}


def _check_lineage_no_dangling(conn) -> Dict[str, Any]:
    """ERROR: every derives/transforms/feeds/lineage edge endpoint resolves."""
    lineage_edges = ("derives", "transforms", "feeds", "lineage")
    placeholders = ",".join("?" for _ in lineage_edges)
    dangling = conn.execute(
        f"""SELECT COUNT(*) FROM edge e
            JOIN edge_type et ON e.edge_type_id=et.id
            WHERE et.name IN ({placeholders})
              AND (NOT EXISTS (SELECT 1 FROM node n WHERE n.id=e.src_node_id)
                OR NOT EXISTS (SELECT 1 FROM node n WHERE n.id=e.dst_node_id))""",
        lineage_edges,
    ).fetchone()[0]
    return {"name": "lineage_no_dangling", "ok": dangling == 0, "severity": "error",
            "detail": f"{dangling} dangling lineage edge(s)"}


def _check_profile_assigned(conn) -> Dict[str, Any]:
    """WARNING: application function/method nodes with no tagged_profile edge."""
    if not _node_type_exists(conn, "profile"):
        return {"name": "profile_assigned", "ok": True, "severity": "warning",
                "detail": "no profiles vocabulary (skipped)"}
    rows = conn.execute(
        """SELECT n.name FROM node n
           JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function', 'method')
             AND NOT EXISTS (
               SELECT 1 FROM edge e JOIN edge_type et ON e.edge_type_id=et.id
               WHERE et.name='tagged_profile' AND e.src_node_id=n.id)"""
    ).fetchall()
    if not rows:
        return {"name": "profile_assigned", "ok": True, "severity": "warning",
                "detail": "all callables profiled"}
    return {"name": "profile_assigned", "ok": False, "severity": "warning",
            "detail": f"{len(rows)} callable(s) without a sub-flow profile"}


def _check_dtype_coverage(conn) -> Dict[str, Any]:
    """WARNING (Phase C-2): the headline data-typing coverage number. Every data
    gate is exactly as strong as this; surfacing it as a tracked metric stops a
    rail from looking green where it is actually blind."""
    cov = dtype_coverage(conn)
    return {"name": "dtype_coverage", "ok": cov["unknown"] == 0,
            "severity": "warning",
            "detail": f"coverage: {cov['pct']}% "
                      f"({cov['typed']}/{cov['total']} typed)",
            "coverage": cov}


_CHECKS = [
    _check_node_types_nonempty,
    _check_no_dangling_edges,
    _check_cards_match_callables,
    _check_commit_sha_set,
    _check_no_undefined_symbols,
    _check_no_isolated_nodes,
    _check_dtype_coverage,
    _check_dtype_present,
    _check_dtype_consistency_e2e,
    _check_lineage_no_dangling,
    _check_profile_assigned,
]


def run(db_path: str) -> Dict[str, Any]:
    """Run all invariants. Returns {ok: bool, checks: [...], report: str}.

    `ok` is True iff every ERROR-severity check passed; failing WARNING checks
    are reported but never flip `ok`.
    """
    conn = sqlite3.connect(db_path)
    try:
        checks: List[Dict[str, Any]] = [c(conn) for c in _CHECKS]
    finally:
        conn.close()
    ok = all(c["ok"] for c in checks if c.get("severity", "error") == "error")
    lines = [f"Self-check: {'PASS' if ok else 'FAIL'} ({db_path})"]
    # Headline coverage metric (Phase C-2), surfaced up top as a tracked number.
    _cov = next((c.get("coverage") for c in checks
                 if c["name"] == "dtype_coverage"), None)
    if _cov is not None:
        lines.append(f"  dtype coverage: {_cov['pct']}% "
                     f"({_cov['typed']}/{_cov['total']} typed)")
    for c in checks:
        if c["ok"]:
            mark = "OK  "
        elif c.get("severity") == "warning":
            mark = "WARN"
        else:
            mark = "XX  "
        lines.append(f"  [{mark}] {c['name']}: {c['detail']}")
    return {"ok": ok, "checks": checks, "report": "\n".join(lines)}


if __name__ == "__main__":
    import sys
    res = run(sys.argv[1])
    print(res["report"])
    sys.exit(0 if res["ok"] else 1)
