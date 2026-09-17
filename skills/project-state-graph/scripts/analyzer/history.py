"""Node history (spec §2.4): snapshot the per-run projection, match it against
the previous run with three deterministic signature layers, append events.

The host owns matching; arbitration is an injected callable (default: none)
and every asserted link must carry evidence. Ambiguities are recorded, never
guessed. All writes go through analyzer.store (append-only tables)."""
from __future__ import annotations

import ast
import hashlib
import importlib as _importlib
import json
import os
from dataclasses import dataclass, field
from typing import Callable

from . import store
from ._host import providers as _providers

# The type sets are the reference providers' (phase 6): one source.
_bs = _importlib.import_module(f"{_providers.__name__}.builtin_symbols")
_bo = _importlib.import_module(f"{_providers.__name__}.builtin_owned")
SYMBOL_TYPES = _bs.SYMBOL_TYPES
NAME_ONLY_TYPES = _bs.NAME_ONLY_TYPES
# Owned nodes (spec §2.2, E0/D1): identity = owner identity + local name.
# dataframe qualified_name = "<fn_qn>:<var>", column = "<owner_qn>.<column>".
OWNED_TYPES = _bo.OWNED_TYPES
EVENT_ORDER = ("node_matched", "node_renamed", "node_moved", "node_changed",
               "node_added", "node_removed", "identity_ambiguous", "identity_asserted")


# The host-owned matching contract (Row/Pair/Ambiguity/Assertion/MatchOutcome,
# match) lives in the package (graph_api, phase 6); re-exported here so the
# analyzer and its tests keep their names.
from ._host import graph_api as _g  # noqa: E402

Row, Pair, Ambiguity, Assertion, MatchOutcome, Arbitrate = (
    _g.Row, _g.Pair, _g.Ambiguity, _g.Assertion, _g.MatchOutcome, _g.Arbitrate)
_changed, _group, _LAYERS, match = _g._changed, _g._group, _g._LAYERS, _g.match


# ── snapshot ─────────────────────────────────────────────────────────────────
def snapshot_run(conn, repo_root: str, run_id: int, providers=None, report: dict | None = None,
                 timeout_s: float = 30.0, file_map=None, isolate: str = "thread") -> int:
    """Write this run's node_snapshot rows (node_key '' placeholder) from the
    providers' observations. Must run BEFORE store.stamp_run: providers see the
    rebuild's rows by run_id IS NULL. Phase 6: the built-in symbol/owned
    identities are providers too; every provider runs through
    providers.run_provider (exception / timeout / schema -> degraded, empty).
    `report` (optional dict) receives {type_id: {degraded, observations, elapsed_s}}.
    `isolate` ("thread" | "subprocess", phase 7) is passed to run_provider."""
    plist = list(providers) if providers is not None else _providers.builtin_providers()
    conn.commit()                                   # the read-only view must see the rebuild
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    ctx = _providers.make_context(db_path, repo_root, run_id, file_map)
    node_qn: dict[int, str] = {}   # node id -> snapshot qualified_name (owners resolve through this)
    seen: dict[tuple[str, str], int] = {}
    n = 0

    def emit(obs):
        nonlocal n
        ntype, qn = obs.node_type, obs.qualified_name
        attrs = dict(obs.attrs)
        if ntype == "column":
            # the owner's SNAPSHOT name (may carry a #2 suffix) wins over the provider's guess
            oid = attrs.pop("owner_node_id", None)
            owner = node_qn.get(oid) if oid is not None else None
            if owner is not None:
                qn = f"{owner}.{obs.name}"
                attrs["owner_qn"] = owner
            else:
                attrs["owner_qn"] = obs.owner_qn
            attrs["name"] = obs.name
        elif obs.owner_qn is not None:
            attrs["owner_qn"], attrs["name"] = obs.owner_qn, obs.name
        # The graph may hold several nodes with one (type, qualified_name) — e.g.
        # `df = read(); df = df.dropna()` binds two dataframe nodes to `df` in one
        # function. Suffix repeats (#2, #3 ...) in the deterministic emit order so
        # every snapshot row has its own name and its own minted key.
        seen[(ntype, qn)] = seen.get((ntype, qn), 0) + 1
        if seen[(ntype, qn)] > 1:
            qn = f"{qn}#{seen[(ntype, qn)]}"
        if obs.node_id is not None:
            node_qn[obs.node_id] = qn
        st, df = obs.signature("struct"), obs.signature("dataflow")
        store.add_node_snapshot(conn, run_id, node_type=ntype, qualified_name=qn, file_path=obs.file_path,
                                line_start=obs.line_start, line_end=obs.line_end,
                                struct_sig=st.value if st else None, dataflow_sig=df.value if df else None,
                                dataflow_trivial=bool(df.trivial) if df else True,
                                attrs={"node_id": obs.node_id, **attrs, "type_id": obs.type_id,
                                       "schema_version": obs.schema_version})
        n += 1

    try:
        for p in plist:
            obs, degraded, elapsed = _providers.run_provider(p, ctx, timeout_s=timeout_s, isolate=isolate)
            if report is not None:
                report[p.type_id] = {"degraded": degraded, "observations": len(obs), "elapsed_s": round(elapsed, 3),
                                     "schema_version": getattr(p, "schema_version", 1)}
            for o in obs:
                emit(o)
            # Commit BETWEEN providers, not only at the end. `emit` writes on
            # `conn` while the next provider reads the same file through
            # `ctx.conn_ro`; an uncommitted write transaction grows with the
            # graph, and on a 222 MB graph it outlived the reader's busy
            # timeout — provledger.owned degraded with "database is locked",
            # 129 dataframe/column nodes lost their node_key and selfcheck's
            # history_key_coverage failed a refresh that had otherwise worked.
            # The docstring above already says the read-only view must see the
            # rebuild; this is the rest of that sentence.
            conn.commit()
    finally:
        ctx.conn_ro.close()
    conn.commit()
    return n


def _rows(conn, run_id: int, *, keyed_only: bool = False) -> list[Row]:
    """Snapshot rows of one run. keyed_only drops '' placeholders — a previous
    run that crashed before resolve must never hand out empty keys."""
    out = []
    for r in conn.execute(
            f"""SELECT id, node_key, node_type, qualified_name, file_path, line_start, line_end,
                       struct_sig, dataflow_sig, dataflow_trivial, attrs_json
                FROM node_snapshot WHERE run_id=? {"AND node_key <> ''" if keyed_only else ""}
                ORDER BY node_type, qualified_name, id""", (run_id,)):
        attrs = json.loads(r[10]) if r[10] else {}
        out.append(Row(r[0], attrs.get("node_id"), r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], bool(r[9]),
                       owner_qn=attrs.get("owner_qn"), name=attrs.get("name")))
    return out


# ── matching (pure) ──────────────────────────────────────────────────────────
# ── resolve: keys + events ───────────────────────────────────────────────────
def _new_key(run_id: int, r: Row) -> str:
    return "nk_" + hashlib.sha1(f"{run_id}:{r.node_type}:{r.qualified_name}".encode()).hexdigest()[:12]


def resolve(conn, run_id: int, arbitrate: Arbitrate | None = None) -> dict:
    """Match this run's snapshot against the previous one, assign node_keys
    (inherited or new), backfill node.node_key, append events in EVENT_ORDER.
    Returns per-event-type counts."""
    prev_run = store.previous_run_id(conn, run_id)
    prev = _rows(conn, prev_run, keyed_only=True) if prev_run is not None else []
    cur = _rows(conn, run_id)
    out = match(prev, cur)
    counts = {k: 0 for k in EVENT_ORDER}
    events: list[tuple[str, str | None, dict]] = []
    keyed: dict[int, str] = {}

    for p in sorted(out.pairs, key=lambda x: x.prev.node_key):
        keyed[p.cur.snapshot_id] = p.prev.node_key
        events.append(("node_matched", p.prev.node_key, {"via": p.via, "prev_run_id": prev_run}))
        if "qualified_name" in p.changed:
            events.append(("node_renamed", p.prev.node_key,
                           {"from": p.prev.qualified_name, "to": p.cur.qualified_name}))
        if "file_path" in p.changed:
            events.append(("node_moved", p.prev.node_key, {"from": p.prev.file_path, "to": p.cur.file_path}))
        sig_changes = [f for f in p.changed if f in ("struct_sig", "dataflow_sig")]
        if sig_changes:
            events.append(("node_changed", p.prev.node_key, {
                "changed": sig_changes,
                "struct_sig": {"from": p.prev.struct_sig, "to": p.cur.struct_sig},
                "dataflow_sig": {"from": p.prev.dataflow_sig, "to": p.cur.dataflow_sig},
                "span": {"from": [p.prev.line_start, p.prev.line_end],
                         "to": [p.cur.line_start, p.cur.line_end]}}))
    for c in out.added:
        keyed[c.snapshot_id] = _new_key(run_id, c)
        events.append(("node_added", keyed[c.snapshot_id],
                       {"qualified_name": c.qualified_name, "node_type": c.node_type}))
    for a in out.ambiguous:
        for c in a.cur:
            keyed[c.snapshot_id] = _new_key(run_id, c)  # provisional: never silently linked
        events.append(("identity_ambiguous", None, {
            "layer": a.layer,
            "prev": [r.qualified_name for r in a.prev], "cur": [r.qualified_name for r in a.cur],
            "prev_keys": [r.node_key for r in a.prev],
            "same_struct_sig": len({r.struct_sig for r in a.prev + a.cur}) == 1,
            "same_dataflow_sig": len({r.dataflow_sig for r in a.prev + a.cur}) == 1}))
    for p in out.removed:
        events.append(("node_removed", p.node_key,
                       {"qualified_name": p.qualified_name, "node_type": p.node_type, "last_seen_run": prev_run}))

    asserted: list[tuple[str, str, dict]] = []
    if out.ambiguous and arbitrate is not None:
        prev_keys = {r.node_key for a in out.ambiguous for r in a.prev}
        cur_by_qn = {r.qualified_name for a in out.ambiguous for r in a.cur}
        for s in arbitrate(out.ambiguous):
            if not s.evidence.strip():
                raise ValueError(f"identity_asserted for {s.cur_qualified_name} requires non-empty evidence")
            if s.chosen_prev_key not in prev_keys or s.cur_qualified_name not in cur_by_qn:
                raise ValueError("assertion references keys outside the ambiguity set")
            asserted.append(("identity_asserted", s.chosen_prev_key,
                             {"cur": s.cur_qualified_name, "evidence": s.evidence, "arbiter": s.arbiter}))

    # Atomic: a resolve that fails midway must leave no partial keys/events
    # behind (the caller's finally-commit would otherwise persist them and the
    # half-keyed run would become a predecessor).
    try:
        for sid, key in keyed.items():
            store.set_snapshot_key(conn, sid, key)
        for r in cur:
            if r.node_id is not None:
                conn.execute("UPDATE node SET node_key=? WHERE id=?", (keyed[r.snapshot_id], r.node_id))
        for et, key, payload in sorted(events, key=lambda e: EVENT_ORDER.index(e[0])):
            store.add_node_event(conn, run_id, et, key, payload, tier="observed")
            counts[et] += 1
        for et, key, payload in asserted:
            store.add_node_event(conn, run_id, et, key, payload, tier="asserted")
            counts[et] += 1
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return counts


def events_of(conn, node_key_or_qn: str) -> list[dict]:
    """All events of one node (by node_key, or by its most recent qualified
    name), each with its run's commit_sha / plan_id / step_id / trigger.
    Events of aborted runs (FL-028) are never returned."""
    key = node_key_or_qn
    if not key.startswith("nk_"):
        row = conn.execute(
            "SELECT node_key FROM node_snapshot WHERE qualified_name=? AND node_key<>'' "
            "ORDER BY run_id DESC, id DESC LIMIT 1", (key,)).fetchone()
        if not row:
            return []
        key = row[0]
    return [{"event_id": r[0], "run_id": r[1], "seq": r[2], "event_type": r[3], "tier": r[4],
             "payload": json.loads(r[5]), "created_at": r[6], "commit_sha": r[7],
             "plan_id": r[8], "step_id": r[9], "trigger": r[10], "extensions_json": r[11]}
            for r in conn.execute(
                """SELECT e.id, e.run_id, e.seq, e.event_type, e.tier, e.payload_json, e.created_at,
                          a.commit_sha, a.plan_id, a.step_id, a.trigger, a.extensions_json
                   FROM node_event e JOIN analysis_run a ON a.id=e.run_id
                   WHERE e.node_key=? AND COALESCE(a.aborted, 0) = 0
                   ORDER BY e.run_id, e.seq""", (key,))]
