"""Structural validation of the refactor-mutation corpus (no matcher needed).

Guards the corpus itself: every case parses, every expectation is legal, both
boundary anchors exist, every generated mutator is registered and keeps the
base parseable, and every symbol named in expect.toml really exists on the
variant's base side (typos in expect.toml would otherwise pass silently).
"""
from __future__ import annotations

import ast
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.corpus import mutate
from tests.corpus.harness import IDENTITY, SEMANTIC, iter_cases

CASES = Path(__file__).parent / "corpus" / "cases"
ALL_CASES = iter_cases(CASES) if CASES.exists() else []
IDS = [c.name for c in ALL_CASES]


def _py_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _qualnames(repo: Path) -> set[str]:
    """Module-relative qualified names of every def/class/method in `repo`."""
    out: set[str] = set()
    for py in _py_files(repo):
        mod = ".".join(py.relative_to(repo).with_suffix("").parts)
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        tree = ast.parse(py.read_text())
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add(f"{mod}.{node.name}")
            elif isinstance(node, ast.ClassDef):
                out.add(f"{mod}.{node.name}")
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.add(f"{mod}.{node.name}.{item.name}")
    return out


def _variant_sides(case, name: str) -> tuple[Path, Path]:
    vdir = case.variants[name]
    if (vdir / "before").exists():
        return vdir / "before", vdir / "after"
    return case.base, vdir


def test_at_least_three_cases():
    assert len(ALL_CASES) >= 3, IDS


@pytest.mark.parametrize("case", ALL_CASES, ids=IDS)
def test_every_python_file_parses(case):
    files = _py_files(case.dir)
    assert files, f"{case.name}: no .py files"
    for py in files:
        ast.parse(py.read_text(), filename=str(py))


@pytest.mark.parametrize("case", ALL_CASES, ids=IDS)
def test_expectations_are_legal(case):
    for section in ("generated", "variants"):
        for name, spec in case.expect.get(section, {}).items():
            assert spec.get("identity") in IDENTITY, f"{case.name}/{name}: identity"
            if spec["identity"] == "preserved":
                assert spec.get("semantic_diff", "expected") in SEMANTIC, f"{case.name}/{name}: semantic_diff"


@pytest.mark.parametrize("case", ALL_CASES, ids=IDS)
def test_boundary_anchors_declared(case):
    v = case.expect.get("variants", {})
    assert v.get("delete_function", {}).get("identity") == "broken", case.name
    assert v.get("swap_two_similar", {}).get("identity") == "ambiguous", case.name


@pytest.mark.parametrize("case", ALL_CASES, ids=IDS)
def test_generated_mutators_registered_and_keep_base_parseable(case):
    gen = case.expect.get("generated", {})
    assert gen, f"{case.name}: no generated variants"
    assert set(gen) <= set(mutate.GENERATED), f"{case.name}: unregistered mutator(s) {set(gen) - set(mutate.GENERATED)}"
    for name in gen:
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "repo"
            shutil.copytree(case.base, dst)
            mutate.GENERATED[name](case.base, dst)
            for py in _py_files(dst):
                ast.parse(py.read_text(), filename=f"{case.name}/generated:{name}/{py.name}")


@pytest.mark.parametrize("case", ALL_CASES, ids=IDS)
def test_declared_symbols_exist_on_base_side(case):
    base_syms = _qualnames(case.base)
    missing = set(case.expect["case"]["symbols"]) - base_syms
    assert not missing, f"{case.name}: case symbols missing from base: {sorted(missing)}"
    for name, spec in case.expect.get("variants", {}).items():
        before, after = _variant_sides(case, name)
        syms = set(spec.get("symbols", case.expect["case"]["symbols"]))
        missing = syms - _qualnames(before)
        assert not missing, f"{case.name}/{name}: symbols missing from base side: {sorted(missing)}"
        for s in spec.get("broken", []):
            assert s in syms, f"{case.name}/{name}: broken {s} not in symbols"
        for s in spec.get("added", []):
            assert s in _qualnames(after), f"{case.name}/{name}: added {s} not present in variant"
            assert s not in _qualnames(before), f"{case.name}/{name}: added {s} already on base side"
        for s in spec.get("renamed", []):
            assert s in syms, f"{case.name}/{name}: renamed {s} not in symbols"
