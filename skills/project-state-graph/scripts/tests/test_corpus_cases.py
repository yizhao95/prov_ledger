"""Structural validation of the refactor-mutation corpus (no matcher needed).

Guards the corpus itself: every case parses, every expectation is legal, both
boundary anchors exist, every generated mutator is registered and really
changes the code (AST, or source text for comment/format-only mutators) while
keeping it parseable, and every symbol named in expect.toml really exists on the
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
def test_generated_mutators_registered(case):
    gen = case.expect.get("generated", {})
    assert gen, f"{case.name}: no generated variants"
    assert set(gen) <= set(mutate.GENERATED), f"{case.name}: unregistered mutator(s) {set(gen) - set(mutate.GENERATED)}"


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


# ── review #35 A-5: a GENERATED mutation must actually change the code ──────

# These three only touch comments / docstrings / layout: the AST may legitimately
# survive, so the evidence of mutation is the source text.
TEXT_ONLY_MUTATORS = {"add_comments", "strip_comments", "reformat"}


def _assert_mutator_changes_code(case, name: str, mutator) -> None:
    """Apply `mutator` to a copy of the base: every file must still parse, and the
    mutation must be visible — a differing ast.dump for semantic mutators, a
    differing source text for TEXT_ONLY_MUTATORS. A no-op mutation fails."""
    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "repo"
        shutil.copytree(case.base, dst)
        mutator(case.base, dst)
        text_changed = ast_changed = False
        for py in _py_files(dst):
            after = py.read_text()
            before = (case.base / py.relative_to(dst)).read_text()
            tree = ast.parse(after, filename=f"{case.name}/generated:{name}/{py.name}")
            text_changed |= after != before
            ast_changed |= ast.dump(tree) != ast.dump(ast.parse(before))
    if name in TEXT_ONLY_MUTATORS:
        assert text_changed, f"{case.name}/generated:{name}: no source text changed"
    else:
        assert ast_changed, f"{case.name}/generated:{name}: no AST changed (no-op mutation)"

@pytest.mark.parametrize("case", ALL_CASES, ids=IDS)
def test_generated_mutators_change_the_code(case):
    for name in case.expect.get("generated", {}):
        _assert_mutator_changes_code(case, name, mutate.GENERATED[name])


def test_noop_mutator_is_rejected():
    def noop(_src: Path, _dst: Path) -> None:
        pass
    with pytest.raises(AssertionError, match="no-op"):
        _assert_mutator_changes_code(ALL_CASES[0], "noop", noop)
    with pytest.raises(AssertionError, match="no source text"):
        _assert_mutator_changes_code(ALL_CASES[0], "reformat", noop)
