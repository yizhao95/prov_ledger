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
