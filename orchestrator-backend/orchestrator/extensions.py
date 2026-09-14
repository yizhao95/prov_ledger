"""Declarative extensions (phase 5, FR-3 first layer): register drift kinds,
analyzer name sets and anchored constraints from ONE JSON file without
touching source — `provledger-extensions.json`.

Rules (spec §4, v2 C.2/C.5.3):
- explicit, never auto-discovered from packages: exactly one file is read —
  `<repo>/provledger-extensions.json`, else `$PROVLEDGER_EXTENSIONS`, else
  `~/skill-workspace/provledger-extensions.json`; never merged.
- ids are namespaced `vendor.name`; a duplicate id fails the load.
- conflicts resolve by `priority` (int, larger first, default 0); two entries
  with the same priority on the same target fail the load.
- reproducible: `fingerprint()` is what analysis_run.extensions_json records.
- a declared drift kind is a deterministic predicate over profile rows, so its
  rows are `source="extension:<id>"` at the observed tier — extensions cannot
  declare asserted/derived.
JSON (not TOML): the backend supports Python 3.10, which has no tomllib.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any

FILE_NAME = "provledger-extensions.json"
ENV_VAR = "PROVLEDGER_EXTENSIONS"
VERSION = 1
ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
DRIFT_OPS = ("delta_gte", "delta_lte", "eq", "changed", "became", "dropped_below", "rose_above")
DRIFT_METRICS = ("dtype", "null_frac", "distinct_count", "row_count", "mean", "min", "max")
VISIBILITY = ("shared", "restricted")


class ExtensionsError(ValueError):
    """The extensions file is malformed or ambiguous; nothing is applied."""


@dataclass(frozen=True)
class DriftKind:
    id: str
    metric: str
    op: str
    value: object
    priority: int = 0
    enabled: bool = True


@dataclass(frozen=True)
class NameSet:
    set: str
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    priority: int = 0


@dataclass(frozen=True)
class ConstraintDecl:
    project: str | None
    statement: str
    subjects: tuple[str, ...]
    keywords: tuple[str, ...] = ()
    why_ref: str | None = None
    why_visibility: str = "shared"


@dataclass(frozen=True)
class Extensions:
    path: str | None
    sha256: str | None
    drift_kinds: tuple[DriftKind, ...]
    namesets: tuple[NameSet, ...]
    constraints: tuple[ConstraintDecl, ...]

    def fingerprint(self) -> dict | None:
        """The shape written to analysis_run.extensions_json; None without a file."""
        if self.path is None:
            return None
        sets: dict[str, list[str]] = {}
        for ns in sorted(self.namesets, key=lambda n: (-n.priority, n.set)):
            sets.setdefault(ns.set, []).extend([*ns.add, *("-" + r for r in ns.remove)])
        return {"path": self.path, "sha256": self.sha256,
                "drift_kinds": [k.id for k in sorted(self.drift_kinds, key=lambda k: (-k.priority, k.id))],
                "namesets": sets, "constraints": len(self.constraints)}


EMPTY = Extensions(None, None, (), (), ())


# ── discovery ─────────────────────────────────────────────────────────────────

def discover(repo_root: str | None = None) -> str | None:
    """The one file that applies: repo, then $PROVLEDGER_EXTENSIONS, then the
    workspace file. First hit wins; nothing is merged."""
    candidates = []
    if repo_root:
        candidates.append(os.path.join(repo_root, FILE_NAME))
    if os.environ.get(ENV_VAR):
        candidates.append(os.environ[ENV_VAR])
    candidates.append(os.path.join(os.path.expanduser("~"), "skill-workspace", FILE_NAME))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


# ── loading + validation ──────────────────────────────────────────────────────

def _err(where: str, msg: str) -> ExtensionsError:
    return ExtensionsError(f"{where}: {msg}")


def _int(where: str, obj: dict, key: str, default: int = 0) -> int:
    v = obj.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise _err(where, f"{key} must be an integer (got {v!r})")
    return v


def _strs(where: str, obj: dict, key: str, required: bool = False) -> tuple[str, ...]:
    v = obj.get(key, [])
    if required and not v:
        raise _err(where, f"{key} is required and must be a non-empty list")
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise _err(where, f"{key} must be a list of non-empty strings")
    return tuple(v)


def _drift_kind(i: int, obj: Any) -> DriftKind:
    where = f"drift_kinds[{i}]"
    if not isinstance(obj, dict):
        raise _err(where, "must be an object")
    kid = obj.get("id")
    if not isinstance(kid, str) or not ID_RE.match(kid):
        raise _err(where, f"id must be namespaced vendor.name ({ID_RE.pattern}), got {kid!r}")
    where = f"drift_kinds[{i}] {kid}"
    metric, op = obj.get("metric"), obj.get("op")
    if metric not in DRIFT_METRICS:
        raise _err(where, f"metric {metric!r} is not one of {list(DRIFT_METRICS)}")
    if op not in DRIFT_OPS:
        raise _err(where, f"op {op!r} is not one of {list(DRIFT_OPS)}")
    if op != "changed" and "value" not in obj:
        raise _err(where, f"op {op!r} needs a value")
    enabled = obj.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _err(where, "enabled must be true/false")
    return DriftKind(id=kid, metric=metric, op=op, value=obj.get("value"), priority=_int(where, obj, "priority"),
                     enabled=enabled)


def _nameset(i: int, obj: Any) -> NameSet:
    where = f"namesets[{i}]"
    if not isinstance(obj, dict) or not isinstance(obj.get("set"), str) or not obj.get("set"):
        raise _err(where, "must be an object with a non-empty 'set' name")
    where = f"namesets[{i}] {obj['set']}"
    ns = NameSet(set=obj["set"], add=_strs(where, obj, "add"), remove=_strs(where, obj, "remove"),
                 priority=_int(where, obj, "priority"))
    if not ns.add and not ns.remove:
        raise _err(where, "needs add and/or remove")
    return ns


def _constraint(i: int, obj: Any) -> ConstraintDecl:
    where = f"constraints[{i}]"
    if not isinstance(obj, dict):
        raise _err(where, "must be an object")
    st = obj.get("statement")
    if not isinstance(st, str) or not st.strip():
        raise _err(where, "statement is required (a non-empty string)")
    vis = obj.get("why_visibility", "shared")
    if vis not in VISIBILITY:
        raise _err(where, f"why_visibility {vis!r} is not one of {list(VISIBILITY)}")
    project = obj.get("project")
    if project is not None and (not isinstance(project, str) or not project):
        raise _err(where, "project must be a non-empty string when given")
    why_ref = obj.get("why_ref")
    if why_ref is not None and not isinstance(why_ref, str):
        raise _err(where, "why_ref must be a string")
    return ConstraintDecl(project=project, statement=st.strip(), subjects=_strs(where, obj, "subjects", required=True),
                          keywords=_strs(where, obj, "keywords"), why_ref=why_ref, why_visibility=vis)


def _check_conflicts(kinds: tuple[DriftKind, ...], sets: tuple[NameSet, ...]) -> None:
    seen: dict[str, int] = {}
    for k in kinds:
        if k.id in seen:
            raise ExtensionsError(f"duplicate id {k.id!r} in drift_kinds — ids are never silently overridden")
        seen[k.id] = 1
    by_target: dict[tuple, str] = {}
    for k in kinds:
        t = ("drift", k.metric, k.op, k.priority)
        if t in by_target:
            raise ExtensionsError(f"drift_kinds {by_target[t]!r} and {k.id!r} share priority {k.priority} on the same "
                                  f"target ({k.metric}/{k.op}) — give one of them a different priority")
        by_target[t] = k.id
    by_set: dict[tuple, int] = {}
    for i, ns in enumerate(sets):
        t = (ns.set, ns.priority)
        if t in by_set:
            raise ExtensionsError(f"namesets[{by_set[t]}] and namesets[{i}] share priority {ns.priority} on set "
                                  f"{ns.set!r} — give one of them a different priority")
        by_set[t] = i


def load(path: str | None) -> Extensions:
    """Parse + validate one extensions file; None -> EMPTY. Any problem raises
    ExtensionsError and nothing is applied."""
    if path is None:
        return EMPTY
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        raise ExtensionsError(f"cannot read {path}: {e}") from e
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ExtensionsError(f"{path}: not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise ExtensionsError(f"{path}: the top level must be an object")
    version = data.get("version", VERSION)
    if version != VERSION:
        raise ExtensionsError(f"{path}: version {version!r} is not supported (this reader understands version {VERSION})")
    for key in ("drift_kinds", "namesets", "constraints"):
        if key in data and not isinstance(data[key], list):
            raise ExtensionsError(f"{path}: {key} must be a list")
    kinds = tuple(_drift_kind(i, o) for i, o in enumerate(data.get("drift_kinds", [])))
    sets = tuple(_nameset(i, o) for i, o in enumerate(data.get("namesets", [])))
    cons = tuple(_constraint(i, o) for i, o in enumerate(data.get("constraints", [])))
    _check_conflicts(kinds, sets)
    return Extensions(path=path, sha256=hashlib.sha256(raw).hexdigest(), drift_kinds=kinds, namesets=sets,
                      constraints=cons)


def current(repo_root: str | None = None) -> Extensions:
    """load(discover(repo_root)) — the one-liner every caller uses."""
    return load(discover(repo_root))
