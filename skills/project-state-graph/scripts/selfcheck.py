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


_TYPING_ALIASES = {"Tuple": "tuple", "List": "list", "Dict": "dict", "Set": "set",
                   "FrozenSet": "frozenset", "Type": "type"}


def _norm_dtype(t: str):
    """-> (outer, params|None). Strips Optional[...] / `| None` / `typing.` and
    maps typing aliases to builtins. `tuple[2]` (a shape signature from
    dataflow_types._infer_return_type) keeps params '[2]'."""
    import re
    t = t.strip().strip("'\"")  # forward-reference strings keep their quotes after ast.unparse
    t = re.sub(r"^(?:typing\.)?Optional\[(.*)\]$", r"\1", t)
    t = re.sub(r"\s*\|\s*None\b", "", t)
    t = re.sub(r"\bNone\s*\|\s*", "", t)
    t = re.sub(r"^typing\.", "", t)
    m = re.match(r"^([A-Za-z_][\w.]*)\s*(\[.*\])?$", t)
    if not m:
        return t, None
    outer = _TYPING_ALIASES.get(m.group(1), m.group(1))
    return outer, m.group(2)


def _dtype_compatible(have: str, want: str) -> bool:
    """FL-013 compatibility rules (all deterministic, all conservative):
    - Optional[X] / X | None on either side is compared as X.
    - An unparameterised generic accepts any refinement (list vs list[dict]).
    - A shape-only signature (tuple[2]) is compatible with any tuple.
    Everything else must match exactly."""
    ho, hp = _norm_dtype(have)
    wo, wp = _norm_dtype(want)
    if ho != wo:
        return False
    if hp is None or wp is None:
        return True
    import re
    if re.fullmatch(r"\[\d+\]", hp) or re.fullmatch(r"\[\d+\]", wp):
        return True
    return hp == wp


def _check_dtype_consistency_e2e(conn) -> Dict[str, Any]:
    """ERROR: a produced data_var's dtype must agree with what each consumer
    expects. We compare the producer's output type against the CONSUMER's declared
    param type (consumes.metadata.expected_type, PSG-C4) — falling back to the
    legacy `type` field when a consumer's expected type wasn't resolved. A mismatch
    of two known concrete types is an end-to-end dtype break.

    FL-013: only `high`-confidence edges are asserted (an `inferred` edge is a
    guess about WHICH callee, or an indirect flow through a subscript/attribute —
    not evidence); `unpacked` edges carry an element, not the value, and are
    skipped; and type comparison goes through _dtype_compatible."""
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
           WHERE t.name='consumes'
             AND (e.confidence IS NULL OR e.confidence = 'high')"""
    ).fetchall():
        info = json.loads(meta) if meta else {}
        if info.get("unpacked"):
            continue
        # PSG-C4: prefer the consumer's declared param type; fall back to legacy.
        want = info.get("expected_type") or info.get("type", "unknown")
        have = produced_type.get(int(dv), "unknown")
        if want not in (None, "unknown") and have not in (None, "unknown") \
                and not _dtype_compatible(have, want):
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


def _check_resolution_coverage(conn) -> Dict[str, Any]:
    """WARNING (Phase 4.3): share of confidence-tagged edges resolved at HIGH vs
    `inferred` (ambiguous same-name). Uses the Phase-3 `confidence` column to make
    edge-resolution incompleteness a visible number — a downstream-completeness
    proxy. Never flips overall ok (a metric, not a hard gate)."""
    total = conn.execute(
        "SELECT COUNT(*) FROM edge WHERE confidence IS NOT NULL").fetchone()[0]
    if total == 0:
        return {"name": "resolution_coverage", "ok": True, "severity": "warning",
                "detail": "no confidence-tagged edges yet"}
    inferred = conn.execute(
        "SELECT COUNT(*) FROM edge WHERE confidence = 'inferred'").fetchone()[0]
    high = total - inferred
    pct = round(100.0 * high / total, 1)
    return {"name": "resolution_coverage", "ok": inferred == 0, "severity": "warning",
            "detail": f"edge resolution: {pct}% high-confidence "
                      f"({high}/{total}); {inferred} ambiguous (inferred)"}


def _check_unguarded_model_inputs(conn) -> Dict[str, Any]:
    """WARNING (Phase 4.2): model inputs fed to .fit/.predict with no validation
    guard. leakage.analyze emits `unguarded_input` nodes; value-level failures
    (e.g. an all-null batch -> predict all-0) need a runtime guard."""
    n = conn.execute(
        """SELECT COUNT(*) FROM node nd JOIN node_type t ON nd.node_type_id=t.id
           WHERE t.name='unguarded_input'"""
    ).fetchone()[0]
    if n == 0:
        return {"name": "unguarded_model_inputs", "ok": True, "severity": "warning",
                "detail": "all model inputs have a validation guard (or none present)"}
    return {"name": "unguarded_model_inputs", "ok": False, "severity": "warning",
            "detail": f"{n} model input(s) fed to fit/predict without a validation guard"}


def _check_no_data_leakage(conn) -> Dict[str, Any]:
    """ERROR (Phase 4.1): a model must not fit AND evaluate on the same-source
    data without a proper split. leakage.analyze emits one `leakage` node per
    finding; any such node fails this gate."""
    import json
    rows = conn.execute(
        """SELECT n.metadata_json FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name='leakage'"""
    ).fetchall()
    if not rows:
        return {"name": "no_data_leakage", "ok": True, "severity": "error",
                "detail": "no data-leakage (train/test dual-use) detected"}
    details = []
    for (meta,) in rows:
        info = json.loads(meta) if meta else {}
        details.append(info.get("detail", "data leakage"))
    sample = "; ".join(details[:3])
    more = "" if len(details) <= 3 else f" (+{len(details) - 3} more)"
    return {"name": "no_data_leakage", "ok": False, "severity": "error",
            "detail": f"{len(details)} data-leakage finding(s): {sample}{more}"}


# ── history layer (spec §2.7) ────────────────────────────────────────────────
_HISTORY_TRIGGERS = ("trg_node_event_no_update", "trg_node_event_no_delete", "trg_node_snapshot_no_delete")
_IDENTITY_TYPES = ("function", "method", "class", "sql_table", "bq_dataset", "api_source",
                   "dataset", "dataframe", "column")


def _latest_resolved_run(conn):
    if not _table_exists(conn, "node_snapshot"):
        return None
    row = conn.execute("SELECT MAX(run_id) FROM node_snapshot WHERE node_key <> ''").fetchone()
    return row[0] if row else None


def _check_history_append_only(conn) -> Dict[str, Any]:
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    missing = [t for t in _HISTORY_TRIGGERS if t not in have]
    return {"name": "history_append_only", "ok": not missing, "severity": "error",
            "detail": ("all 3 append-only triggers present" if not missing
                       else f"missing trigger(s): {', '.join(missing)}")}


def _check_history_key_coverage(conn) -> Dict[str, Any]:
    """Every identity-bearing node of the latest build carries a node_key."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(node)")}
    if "node_key" not in cols:
        return {"name": "history_key_coverage", "ok": False, "severity": "error",
                "detail": "node.node_key column missing (graph built before the history layer; rebuild)"}
    run = conn.execute("SELECT MAX(id) FROM analysis_run").fetchone()[0]
    if run is None:
        return {"name": "history_key_coverage", "ok": True, "severity": "error", "detail": "no runs"}
    # hand-built / older graphs may lack node.run_id: then every node is "this run"
    run_filter = "n.run_id=? AND " if "run_id" in cols else ""
    params = ((run,) if "run_id" in cols else ()) + _IDENTITY_TYPES
    where = f"""{run_filter}t.name IN ({','.join('?' * len(_IDENTITY_TYPES))})
              AND (n.node_key IS NULL OR n.node_key='')"""
    rows = conn.execute(
        f"""SELECT COALESCE(n.qualified_name, n.name) FROM node n JOIN node_type t ON n.node_type_id=t.id
            WHERE {where} ORDER BY 1 LIMIT 5""", params).fetchall()
    total = conn.execute(
        f"""SELECT COUNT(*) FROM node n JOIN node_type t ON n.node_type_id=t.id WHERE {where}""",
        params).fetchone()[0]
    return {"name": "history_key_coverage", "ok": total == 0, "severity": "error",
            "detail": (f"every identity-bearing node of run {run} has a node_key" if total == 0
                       else f"{total} node(s) of run {run} without node_key, e.g. {', '.join(r[0] for r in rows)}")}


def _check_history_ambiguous(conn) -> Dict[str, Any]:
    run = _latest_resolved_run(conn)
    if run is None:
        return {"name": "history_ambiguous", "ok": True, "severity": "warning", "detail": "no resolved history run"}
    n = conn.execute("SELECT COUNT(*) FROM node_event WHERE run_id=? AND event_type='identity_ambiguous'", (run,)).fetchone()[0]
    return {"name": "history_ambiguous", "ok": n == 0, "severity": "warning",
            "detail": f"{n} identity_ambiguous in run {run}" + ("" if n == 0 else " (arbitrate or accept the break)")}


def _check_history_broken_ratio(conn) -> Dict[str, Any]:
    run = _latest_resolved_run(conn)
    if run is None:
        return {"name": "history_broken_ratio", "ok": True, "severity": "warning", "detail": "no resolved history run"}
    removed = conn.execute("SELECT COUNT(*) FROM node_event WHERE run_id=? AND event_type='node_removed'", (run,)).fetchone()[0]
    prev = conn.execute(
        "SELECT MAX(run_id) FROM node_snapshot WHERE node_key <> '' AND run_id < ?", (run,)).fetchone()[0]
    prev_n = conn.execute("SELECT COUNT(*) FROM node_snapshot WHERE run_id=? AND node_key <> ''", (prev,)).fetchone()[0] if prev else 0
    pct = (100.0 * removed / prev_n) if prev_n else 0.0
    return {"name": "history_broken_ratio", "ok": removed == 0, "severity": "warning",
            "detail": f"{removed}/{prev_n} previous nodes removed in run {run} ({pct:.1f}%)"}


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
    _check_resolution_coverage,
    _check_unguarded_model_inputs,
    _check_no_data_leakage,
    _check_history_append_only,
    _check_history_key_coverage,
    _check_history_ambiguous,
    _check_history_broken_ratio,
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
