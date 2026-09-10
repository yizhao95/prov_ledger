"""Refactor-mutation corpus harness. Pure, DB-independent: the unit under test
is a Matcher over NodeObs lists; extraction is injected."""
from __future__ import annotations

import shutil
import tempfile
import tomllib
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
        else:
            # An ambiguity elsewhere in the repo must not satisfy the
            # expectation: every declared symbol has to sit on the prev side.
            unbound = [s for s in symbols if s not in amb_prev]
            if unbound:
                fails.append(f"ambiguous: {unbound} not on the prev side of any ambiguity")
        if removed & set(symbols):
            fails.append(f"ambiguous case must not silently remove {sorted(removed)}")
    return fails


# ── cases on disk ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Case:
    name: str
    dir: Path
    base: Path
    expect: dict
    variants: dict[str, Path]


def load_case(case_dir: Path) -> Case:
    """Load `<case_dir>/expect.toml`; every `variants/<m>/` dir must be declared."""
    expect = tomllib.loads((case_dir / "expect.toml").read_text())
    vdir = case_dir / "variants"
    dirs = {p.name: p for p in sorted(vdir.iterdir()) if p.is_dir()} if vdir.exists() else {}
    declared = set(expect.get("variants", {}))
    if set(dirs) != declared:
        raise ValueError(f"{case_dir.name}: variants dirs {sorted(dirs)} != expect {sorted(declared)}")
    return Case(expect["case"]["name"], case_dir, case_dir / "base", expect, dirs)


def iter_cases(root: Path) -> list[Case]:
    return [load_case(p) for p in sorted(root.iterdir()) if (p / "expect.toml").exists()]


Mutator = Callable[[Path, Path], None]


def run_case(case: Case, extractor: Extractor, matcher: Matcher,
             generated: dict[str, Mutator] | None = None) -> list[str]:
    """Run every generated + hand-written variant of `case`; return failures.

    The base is extracted twice and must compare equal (extractor determinism).
    Generated variants copy the base to a temp dir and apply the registered
    mutator; hand-written variants with `before/` use it as their own base.
    A variant spec may override `symbols`.
    """
    fails: list[str] = []
    symbols = case.expect["case"]["symbols"]
    b1, b2 = extractor(case.base), extractor(case.base)
    if sorted(b1) != sorted(b2):
        return ["determinism: two extractions of base differ"]
    have = {o.qualified_name for o in b1}
    missing = [s for s in symbols if s not in have]
    if missing:
        fails.append(f"base symbols not extracted: {missing}")
    for name, spec in case.expect.get("generated", {}).items():
        if not generated or name not in generated:
            fails.append(f"generated:{name}: no mutator registered")
            continue
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "repo"
            shutil.copytree(case.base, dst)
            generated[name](case.base, dst)
            v = extractor(dst)
            syms = spec.get("symbols", symbols)
            fails += [f"generated:{name}: {f}" for f in check(syms, b1, v, matcher(b1, v), spec)]
    for name, spec in case.expect.get("variants", {}).items():
        vdir = case.variants[name]
        if (vdir / "before").exists():
            base_obs = extractor(vdir / "before")
            v = extractor(vdir / "after")
        else:
            base_obs, v = b1, extractor(vdir)
        syms = spec.get("symbols", symbols)
        fails += [f"{name}: {f}" for f in check(syms, base_obs, v, matcher(base_obs, v), spec)]
    return fails
