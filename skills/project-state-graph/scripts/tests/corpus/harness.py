"""Refactor-mutation corpus harness. Pure, DB-independent: the unit under test
is a Matcher over NodeObs lists; extraction is injected."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

IDENTITY = ("preserved", "broken", "ambiguous")
SEMANTIC = ("none", "callers", "expected")


@dataclass(frozen=True, order=True)
class NodeObs:
    node_type: str
    qualified_name: str
    file_path: str
    struct_sig: str
    dataflow_sig: str
    dataflow_trivial: bool


@dataclass(frozen=True)
class Pair:
    prev: NodeObs
    cur: NodeObs
    via: str
    changed: tuple[str, ...]


@dataclass
class MatchResult:
    pairs: list[Pair]
    removed: list[NodeObs]
    added: list[NodeObs]
    ambiguous: list[tuple[list[NodeObs], list[NodeObs]]] = field(default_factory=list)


Matcher = Callable[[list[NodeObs], list[NodeObs]], MatchResult]
Extractor = Callable[[Path], list[NodeObs]]


def check(symbols, base, variant, result: MatchResult, expect: dict) -> list[str]:
    """Return failure descriptions ([] == the variant met its expectation).

    Built-in must_not: preserved => no node_removed / identity_ambiguous;
    semantic_diff=none => no node_changed (struct_sig) on any symbol.
    """
    identity = expect.get("identity")
    if identity not in IDENTITY:
        return [f"expect.identity must be one of {IDENTITY}, got {identity!r}"]
    fails: list[str] = []
    by_prev = {p.prev.qualified_name: p for p in result.pairs}
    removed = {o.qualified_name for o in result.removed}
    amb_prev = {o.qualified_name for prev, _ in result.ambiguous for o in prev}

    if identity == "preserved":
        for s in symbols:
            if s in removed:
                fails.append(f"{s}: node_removed (must_not for preserved)")
            elif s in amb_prev:
                fails.append(f"{s}: identity_ambiguous (must_not for preserved)")
            elif s not in by_prev:
                fails.append(f"{s}: no pair and no explicit removed/ambiguous — silent loss")
        via = expect.get("via")
        if via:
            wrong = [s for s in symbols if s in by_prev and by_prev[s].via != via]
            if wrong:
                fails.append(f"via {via} expected, got {[(s, by_prev[s].via) for s in wrong]}")
        sem = expect.get("semantic_diff", "expected")
        if sem not in SEMANTIC:
            fails.append(f"semantic_diff must be one of {SEMANTIC}")
        changed = [s for s in symbols if s in by_prev and "struct_sig" in by_prev[s].changed]
        if sem == "none" and changed:
            fails.append(f"node_changed on {changed} (semantic_diff=none)")
        if sem == "callers":
            targets = set(expect.get("renamed", []))
            bad = [s for s in changed if s in targets]
            if bad:
                fails.append(f"renamed target(s) {bad} must not be node_changed")
        added = {o.qualified_name for o in result.added}
        for extra in expect.get("added", []):
            if extra not in added:
                fails.append(f"expected node_added {extra}")
    elif identity == "broken":
        want = set(expect.get("broken", []))
        if want != removed & set(symbols):
            fails.append(f"broken: expected {sorted(want)}, got removed {sorted(removed)}")
    else:  # ambiguous
        if not result.ambiguous:
            fails.append("expected identity_ambiguous, none produced")
        if removed & set(symbols):
            fails.append(f"ambiguous case must not silently remove {sorted(removed)}")
    return fails
