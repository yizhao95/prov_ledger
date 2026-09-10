"""Node history (spec §2.4): snapshot the per-run projection, match it against
the previous run with three deterministic signature layers, append events.

The host owns matching; arbitration is an injected callable (default: none)
and every asserted link must carry evidence. Ambiguities are recorded, never
guessed. All writes go through analyzer.store (append-only tables)."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Callable

from . import signatures, store

SYMBOL_TYPES = ("function", "method", "class")
NAME_ONLY_TYPES = ("sql_table", "bq_dataset", "api_source", "dataset")
# Owned nodes (spec §2.2, E0/D1): identity = owner identity + local name.
# dataframe qualified_name = "<fn_qn>:<var>", column = "<owner_qn>.<column>".
OWNED_TYPES = ("dataframe", "column")
EVENT_ORDER = ("node_matched", "node_renamed", "node_moved", "node_changed",
               "node_added", "node_removed", "identity_ambiguous", "identity_asserted")


@dataclass(frozen=True)
class Row:
    """One node_snapshot row (the matcher's only input)."""
    snapshot_id: int
    node_id: int | None
    node_key: str
    node_type: str
    qualified_name: str
    file_path: str | None
    line_start: int | None
    line_end: int | None
    struct_sig: str | None
    dataflow_sig: str | None
    dataflow_trivial: bool
    owner_qn: str | None = None   # OWNED_TYPES only: the owner's qualified_name
    name: str | None = None       # OWNED_TYPES only: the local name (var / column)


@dataclass(frozen=True)
class Pair:
    prev: Row
    cur: Row
    via: str
    changed: tuple[str, ...]


@dataclass(frozen=True)
class Ambiguity:
    layer: str
    prev: tuple[Row, ...]
    cur: tuple[Row, ...]


@dataclass(frozen=True)
class Assertion:
    cur_qualified_name: str
    chosen_prev_key: str
    evidence: str
    arbiter: str


@dataclass
class MatchOutcome:
    pairs: list[Pair]
    removed: list[Row]
    added: list[Row]
    ambiguous: list[Ambiguity] = field(default_factory=list)


Arbitrate = Callable[[list[Ambiguity]], list[Assertion]]


# ── snapshot ─────────────────────────────────────────────────────────────────
def _defs_of(tree: ast.Module) -> list[ast.AST]:
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def _find_def(defs: list[ast.AST], line_start: int | None, name: str | None):
    """The def recorded by py_ast: same start line, else the unique def of that
    name (decorators can shift the recorded line by one or two)."""
    if line_start is not None:
        for d in defs:
            if d.lineno == line_start:
                return d
    if name:
        named = [d for d in defs if d.name == name]
        if len(named) == 1:
            return named[0]
        if line_start is not None and named:
            return min(named, key=lambda d: abs(d.lineno - line_start))
    return None


def _owner_fn(fn_spans: list[tuple[int, int | None, str]], line: int | None) -> str | None:
    """Innermost function/method (by start line) whose span contains `line`."""
    if line is None:
        return None
    best = None
    for ls, le, qn in fn_spans:
        if ls <= line and (le is None or line <= le) and (best is None or ls > best[0]):
            best = (ls, qn)
    return best[1] if best else None


def snapshot_run(conn, repo_root: str, run_id: int) -> int:
    """Write this run's node_snapshot rows (node_key '' placeholder). Must run
    BEFORE store.stamp_run: it selects the rebuild's rows by run_id IS NULL.

    Symbols get struct_sig (source AST) + dataflow_sig (edges); name-only data
    nodes carry their qualified_name; dataframes are keyed to the function that
    binds them and columns to their has_column owner (spec §2.2)."""
    types = SYMBOL_TYPES + NAME_ONLY_TYPES + OWNED_TYPES
    rows = conn.execute(
        f"""SELECT n.id, t.name, n.qualified_name, n.name, n.file_path, n.line_start, n.line_end, n.dtype
            FROM node n JOIN node_type t ON n.node_type_id=t.id
            WHERE t.name IN ({','.join('?' * len(types))}) AND n.run_id IS NULL""", types).fetchall()
    by_type: dict[str, list] = {}
    for r in rows:
        by_type.setdefault(r[1], []).append(r)
    fn_spans: dict[str, list[tuple[int, int | None, str]]] = {}
    for ntype in ("function", "method"):
        for nid, _t, qn, name, path, ls, le, _d in by_type.get(ntype, []):
            if path and ls:
                fn_spans.setdefault(path, []).append((ls, le, qn or name))
    owner_of_col = {dst: src for src, dst in conn.execute(
        """SELECT e.src_node_id, e.dst_node_id FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           WHERE t.name='has_column'""")}
    node_qn: dict[int, str] = {}   # node id -> snapshot qualified_name (owners resolve through this)
    trees: dict[str, list[ast.AST]] = {}
    seen: dict[tuple[str, str], int] = {}
    n = 0

    def emit(nid, ntype, qn, path, ls, le, struct, df_sig, trivial, attrs):
        nonlocal n
        # The graph may hold several nodes with one (type, qualified_name) — e.g.
        # `df = read(); df = df.dropna()` binds two dataframe nodes to `df` in one
        # function. Suffix repeats (#2, #3 ...) in the deterministic emit order so
        # every snapshot row has its own name and its own minted key.
        seen[(ntype, qn)] = seen.get((ntype, qn), 0) + 1
        if seen[(ntype, qn)] > 1:
            qn = f"{qn}#{seen[(ntype, qn)]}"
        node_qn[nid] = qn
        store.add_node_snapshot(conn, run_id, node_type=ntype, qualified_name=qn, file_path=path,
                                line_start=ls, line_end=le, struct_sig=struct, dataflow_sig=df_sig,
                                dataflow_trivial=trivial, attrs={"node_id": nid, **attrs})
        n += 1

    for ntype in SYMBOL_TYPES + NAME_ONLY_TYPES:
        for nid, _t, qn, name, path, ls, le, _d in sorted(by_type.get(ntype, []), key=lambda r: (r[2] or r[3], r[0])):
            struct = df_sig = None
            trivial, attrs = True, {}
            if ntype in SYMBOL_TYPES and path:
                if path not in trees:
                    try:
                        src = open(os.path.join(repo_root, path), encoding="utf-8").read()
                        trees[path] = _defs_of(ast.parse(src))
                    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
                        trees[path] = []
                node = _find_def(trees[path], ls, name)
                if node is not None:
                    struct = signatures.struct_sig(node)
                df_sig, trivial, attrs = signatures.dataflow_sig(conn, nid)
            emit(nid, ntype, qn or name, path, ls, le, struct, df_sig, trivial, attrs)
    for nid, _t, _qn, name, path, ls, le, _d in sorted(by_type.get("dataframe", []), key=lambda r: (r[4] or "", r[5] or 0, r[0])):
        owner = _owner_fn(fn_spans.get(path, []), ls) or (path or "?")
        emit(nid, "dataframe", f"{owner}:{name}", path, ls, le, None, None, True,
             {"owner_qn": owner, "name": name})
    for nid, _t, _qn, name, path, ls, le, dtype in sorted(by_type.get("column", []), key=lambda r: (r[0],)):
        oid = owner_of_col.get(nid)
        owner = node_qn.get(oid) if oid is not None else None
        if owner is None and oid is not None:
            row = conn.execute("SELECT qualified_name, name FROM node WHERE id=?", (oid,)).fetchone()
            owner = (row[0] or row[1]) if row else None
        owner = owner or (path or "?")
        emit(nid, "column", f"{owner}.{name}", path, ls, le, None, None, True,
             {"owner_qn": owner, "name": name, "dtype": dtype or "unknown"})
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
def _changed(p: Row, c: Row) -> tuple[str, ...]:
    return tuple(f for f in ("qualified_name", "file_path", "struct_sig", "dataflow_sig")
                 if getattr(p, f) != getattr(c, f))


def _group(rows: list[Row], key) -> dict:
    g: dict = {}
    for r in rows:
        k = key(r)
        if k is not None:
            g.setdefault(k, []).append(r)
    return g


_LAYERS = (
    ("struct_sig", lambda r: (r.node_type, r.struct_sig) if r.struct_sig else None),
    ("dataflow_sig", lambda r: (r.node_type, r.dataflow_sig)
     if r.dataflow_sig and not r.dataflow_trivial else None),
)


def match(prev: list[Row], cur: list[Row]) -> MatchOutcome:
    """Layer 1 exact (node_type, qualified_name); layer 2 struct_sig groups that
    are exactly 1:1; layer 3 non-trivial dataflow_sig groups 1:1; then the owner
    layer for OWNED_TYPES (same local name, owners paired in this match — to a
    fixpoint, so function -> dataframe -> column chains resolve). Any signature
    group larger than 1:1 is an Ambiguity — recorded, never linked. Pure:
    inputs are not mutated."""
    pairs: list[Pair] = []
    ambiguous: list[Ambiguity] = []
    cur_by_qn = {(r.node_type, r.qualified_name): r for r in cur}
    used_cur: set[int] = set()
    rest_prev: list[Row] = []
    for p in prev:
        c = cur_by_qn.get((p.node_type, p.qualified_name))
        if c is not None and c.snapshot_id not in used_cur:
            pairs.append(Pair(p, c, "qualname", _changed(p, c)))
            used_cur.add(c.snapshot_id)
        else:
            rest_prev.append(p)
    rest_cur = [c for c in cur if c.snapshot_id not in used_cur]
    for layer, key in _LAYERS:
        gp, gc = _group(rest_prev, key), _group(rest_cur, key)
        for k in sorted(set(gp) & set(gc), key=str):
            ps, cs = gp[k], gc[k]
            if len(ps) == 1 and len(cs) == 1:
                pairs.append(Pair(ps[0], cs[0], layer, _changed(ps[0], cs[0])))
            else:
                ambiguous.append(Ambiguity(layer, tuple(ps), tuple(cs)))
            for r in ps:
                rest_prev.remove(r)
            for r in cs:
                rest_cur.remove(r)
    # owner inheritance (E0): a dataframe/column follows its owner's identity.
    cur_of_prev_qn = {p.prev.qualified_name: p.cur.qualified_name for p in pairs}
    progress = True
    while progress:
        progress = False
        cur_by_owner = {(r.node_type, r.owner_qn, r.name): r for r in rest_cur if r.owner_qn}
        for p in list(rest_prev):
            if not p.owner_qn:
                continue
            cur_owner = cur_of_prev_qn.get(p.owner_qn)
            c = cur_by_owner.get((p.node_type, cur_owner, p.name)) if cur_owner else None
            if c is None:
                continue
            pairs.append(Pair(p, c, "owner", _changed(p, c)))
            rest_prev.remove(p)
            rest_cur.remove(c)
            cur_of_prev_qn[p.qualified_name] = c.qualified_name
            progress = True
    return MatchOutcome(pairs, rest_prev, rest_cur, ambiguous)


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
    name), each with its run's commit_sha / plan_id / step_id / trigger."""
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
             "plan_id": r[8], "step_id": r[9], "trigger": r[10]}
            for r in conn.execute(
                """SELECT e.id, e.run_id, e.seq, e.event_type, e.tier, e.payload_json, e.created_at,
                          a.commit_sha, a.plan_id, a.step_id, a.trigger
                   FROM node_event e JOIN analysis_run a ON a.id=e.run_id
                   WHERE e.node_key=? ORDER BY e.run_id, e.seq""", (key,))]
