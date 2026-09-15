"""Host API for node-type providers (phase 6, FR-3 second layer).

**Host owns identity, history, tier and propagation; a plugin owns extraction
and interpretation.** A `NodeTypeProvider` looks at a repository (through a
read-only `ExtractionContext`) and returns `NodeObservation`s: what it saw,
with per-layer `Signature`s the host may match on and free attributes that
must satisfy the provider's own `AttrSchema`. Matching, node keys, events and
tiers are decided by the host (`graph_api.match` is the matcher the built-in
reference providers and the conformance suite share).

Every observation produced by `extract()` is *observed*-tier by construction —
a provider cannot declare a tier (`tier` is always a forbidden attribute).
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, TypedDict, runtime_checkable

TYPE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
KNOWN_LAYERS = ("qualname", "struct", "dataflow")
STABILITY = ("preserved", "broken", "ambiguous")
MUTATIONS = ("rename_variable", "rename_function", "strip_comments", "add_comments", "reformat",
             "move_file", "extract_function", "change_attr", "delete_function", "swap_two_similar")
_TYPES = {"str": str, "int": int, "float": (int, float), "bool": bool, "list": list, "dict": dict}


@dataclass(frozen=True)
class Signature:
    """One identity layer of an observation. The host matches only on known
    layers (`KNOWN_LAYERS`) or `x-` custom layers it was told about; a value of
    None means the layer is unavailable; `trivial` layers never match."""
    layer: str
    value: str | None
    trivial: bool = False


@dataclass(frozen=True)
class NodeObservation:
    type_id: str                       # "vendor.name" (built-ins: provledger.symbol / provledger.owned)
    node_type: str                     # written to node_snapshot.node_type
    qualified_name: str
    file_path: str | None
    line_start: int | None
    line_end: int | None
    signatures: tuple[Signature, ...]
    attrs: Mapping[str, object] = field(default_factory=dict)
    owner_qn: str | None = None        # OWNED types: the owner's qualified_name
    name: str | None = None            # OWNED types: the local name
    node_id: int | None = None         # the graph's node row, when there is one (host backfills node.node_key)
    schema_version: int = 1

    def signature(self, layer: str) -> Signature | None:
        for s in self.signatures:
            if s.layer == layer:
                return s
        return None


@dataclass(frozen=True)
class ExtractionContext:
    """What a provider gets: a read-only graph connection, the repo, this run's
    id, the file map and a read-only query for this run's node rows by type."""
    repo_root: str
    conn_ro: sqlite3.Connection
    run_id: int
    file_map: Mapping[str, int]
    node_rows: Callable[[tuple[str, ...]], list]


class AttrSchema(TypedDict, total=False):
    required: list[str]
    types: dict[str, str]              # "str" | "int" | "float" | "bool" | "list" | "dict"
    forbidden: list[str]               # the host always adds "tier"


@runtime_checkable
class NodeTypeProvider(Protocol):
    type_id: str
    schema_version: int
    requires: tuple[str, ...]          # capabilities: "runtime_capture" | "network" | "llm" (host offers none yet)

    def extract(self, ctx: ExtractionContext) -> list[NodeObservation]: ...
    def attributes_schema(self) -> AttrSchema: ...
    def declared_stability(self) -> dict[str, str]: ...      # MUTATION -> STABILITY


def normalize_schema(schema: Mapping | None) -> AttrSchema:
    """A complete schema with the host's rule applied: `tier` is forbidden."""
    s = dict(schema or {})
    forbidden = list(s.get("forbidden") or [])
    if "tier" not in forbidden:
        forbidden.append("tier")
    return {"required": list(s.get("required") or []), "types": dict(s.get("types") or {}), "forbidden": forbidden}


def validate_attrs(schema: Mapping | None, attrs: Mapping[str, object]) -> list[dict]:
    """[] when `attrs` satisfies `schema`; otherwise one {kind, attr, detail}
    per violation — kind in required / type / forbidden."""
    s = normalize_schema(schema)
    errors: list[dict] = []
    for name in s["required"]:
        if name not in attrs:
            errors.append({"kind": "required", "attr": name, "detail": f"required attribute {name!r} is missing"})
    for name, tname in s["types"].items():
        if name not in attrs:
            continue
        want = _TYPES.get(tname)
        value = attrs[name]
        ok = want is not None and isinstance(value, want) and not (tname != "bool" and isinstance(value, bool))
        if not ok:
            errors.append({"kind": "type", "attr": name,
                           "detail": f"attribute {name!r} must be {tname}, got {type(value).__name__}"})
    for name in s["forbidden"]:
        if name in attrs:
            errors.append({"kind": "forbidden", "attr": name,
                           "detail": f"attribute {name!r} is forbidden"
                                     + (" — tier is decided by the host, never by a plugin" if name == "tier" else "")})
    return errors


def observation_key(obs: NodeObservation) -> tuple:
    """Deterministic ordering for observations (conformance compares sorted lists)."""
    return (obs.type_id, obs.node_type, obs.qualified_name, obs.file_path or "", obs.line_start or 0)


def observation_json(obs: NodeObservation) -> str:
    """Canonical JSON of one observation (node_id excluded: it is run-local)."""
    return json.dumps({
        "type_id": obs.type_id, "node_type": obs.node_type, "qualified_name": obs.qualified_name,
        "file_path": obs.file_path, "line_start": obs.line_start, "line_end": obs.line_end,
        "signatures": [[s.layer, s.value, s.trivial] for s in obs.signatures],
        "attrs": obs.attrs, "owner_qn": obs.owner_qn, "name": obs.name, "schema_version": obs.schema_version,
    }, sort_keys=True, default=str)


# ── the host matcher (moved from the analyzer's history.py, phase 6) ──────────
# Row is the host's matching row (a node_snapshot projection); providers never
# build Rows — the host does, from their NodeObservations.
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
    context: str | None = None    # phase 8: ±10 source lines for an arbiter; never read by match()


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


@runtime_checkable
class Arbiter(Protocol):
    """Phase 7: something that may link ambiguous identities. The analyzer
    calls arbitrate() only after provledger.testing.calibration's gate has
    passed for `arbiter_id` (consistency 1.0, accuracy >= 0.9 on >= 10
    labelled items, report sha == calibration file); every Assertion must
    carry non-empty evidence or the resolver refuses it."""
    arbiter_id: str

    def arbitrate(self, ambiguities: list[Ambiguity]) -> list[Assertion]: ...


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
