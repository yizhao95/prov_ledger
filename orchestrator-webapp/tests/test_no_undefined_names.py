"""Every global name the dashboard's modules load actually exists.

DP phase 2c found this the way it is meant to be found: the review's refresh
failed `no_undefined_symbols` on `_recent_kind()` in `app/queries.py`. The
helper and its `RECENT_KINDS` table were introduced by one commit and removed
by the next, which left `trace_strip()` calling two names that no longer
existed. Nothing called `trace_strip`, so the NameError never fired — **dead
code with a bug in it looks exactly like working code**, and only a whole-module
check can tell the difference.

This is a cheap, local version of the analyzer's hard gate, so the webapp suite
catches it in seconds instead of thirteen minutes into a graph refresh.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
MODULES = ("queries.py", "main.py", "vocab.py")


def _module_scope(tree: ast.Module) -> set[str]:
    """Everything a function body may legitimately reach at module level."""
    names = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "__package__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.Import):
            names.update((a.asname or a.name.split(".")[0]) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update((a.asname or a.name) for a in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def _bound_locally(fn: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name.split(".")[0]) for a in node.names)
    return names


def _top_level_functions(tree: ast.Module) -> list[ast.AST]:
    """Module-level functions and class methods. Nested functions are checked
    as part of their enclosing one: their free variables are that function's
    locals, which is exactly what Python does and what the first version of
    this check got wrong (five false positives in `_build_context`)."""
    out: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(node)
        elif isinstance(node, ast.ClassDef):
            out.extend(n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return out


@pytest.mark.parametrize("module", MODULES)
def test_no_function_loads_a_name_that_does_not_exist(module):
    path = APP / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scope = _module_scope(tree)
    missing: list[str] = []
    for fn in _top_level_functions(tree):
        # the whole subtree's bindings, so a nested function's free variables
        # resolve against the enclosing function the way Python resolves them
        local = _bound_locally(fn)
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id not in local and node.id not in scope:
                    missing.append(f"{module}:{node.lineno} {fn.name}() loads undefined {node.id!r}")
    assert not missing, "\n".join(sorted(set(missing)))
