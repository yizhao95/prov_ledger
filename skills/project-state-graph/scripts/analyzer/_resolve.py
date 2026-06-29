"""Shared qualified-name resolver + AST helpers (PSG-C2/C3/C5 foundation).

The analyzers historically resolved call/reference targets by SIMPLE NAME with
last-wins (`{name: id}`), so two functions named `run`/`load` bound every edge to
one arbitrary node. This module centralizes resolution so a name is resolved by
module/class-qualified name first, and — when still ambiguous — returns ALL
candidates tagged `inferred` so the caller can emit one edge per candidate with
confidence (never an arbitrary wrong `high` edge).
"""
from __future__ import annotations

import ast
from typing import Optional


def iter_funcs(tree, module):
    """Yield (fn_node, qualified_name) for top-level functions and class methods,
    matching py_ast's node set + qualified_name scheme (module.fn, module.Class.method).

    Lets an analyzer resolve the ENCLOSING function to its OWN unique node id
    (idx.by_qual[qual]) instead of a global last-wins simple-name lookup.
    """
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node, f"{module}.{node.name}"
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield item, f"{module}.{node.name}.{item.name}"


def build_index(conn) -> "CallableIndex":
    """Load all callable nodes once into a resolver index."""
    rows = conn.execute(
        """SELECT n.id, n.name, n.qualified_name
           FROM node n JOIN node_type t ON n.node_type_id = t.id
           WHERE t.name IN ('function', 'method')"""
    ).fetchall()
    by_qual: dict[str, int] = {}
    by_name: dict[str, list[int]] = {}
    for nid, name, qual in rows:
        if qual:
            by_qual[qual] = int(nid)
        by_name.setdefault(name, []).append(int(nid))
    return CallableIndex(by_qual, by_name)


class CallableIndex:
    def __init__(self, by_qual: dict[str, int], by_name: dict[str, list[int]]):
        self.by_qual = by_qual
        self.by_name = by_name

    def resolve(self, name: str, *, module: Optional[str] = None,
                klass: Optional[str] = None) -> list[tuple[int, str]]:
        """Resolve a callee name to a list of (node_id, confidence).

        Order: exact module[.class].name (high) -> unique simple name (high) ->
        N ambiguous candidates (each inferred) -> [] when unknown.
        """
        # 1. qualified by the caller's module/class context
        candidates_qual = []
        if module and klass:
            candidates_qual.append(f"{module}.{klass}.{name}")
        if module:
            candidates_qual.append(f"{module}.{name}")
        for q in candidates_qual:
            nid = self.by_qual.get(q)
            if nid is not None:
                return [(nid, "high")]

        # 2/3. simple-name fallback
        ids = self.by_name.get(name, [])
        if len(ids) == 1:
            return [(ids[0], "high")]
        if len(ids) > 1:
            return [(i, "inferred") for i in ids]
        return []

    def resolve_one(self, name: str, *, module: Optional[str] = None,
                    klass: Optional[str] = None) -> Optional[int]:
        """Convenience: the single high-confidence id, else None (ambiguous/unknown)."""
        r = self.resolve(name, module=module, klass=klass)
        if len(r) == 1 and r[0][1] == "high":
            return r[0][0]
        return None


# ── AST helpers (hoisted from the per-analyzer copies) ───────────────────────────
def call_name(func) -> Optional[str]:
    """The simple callee name of a Call.func: Name -> id, Attribute -> attr."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def string_value(node) -> Optional[str]:
    """The str value of an ast.Constant string literal, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def attr_root(node) -> Optional[str]:
    """Root name of an attribute chain: a.b.c -> 'a'; a Name -> its id."""
    cur = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if isinstance(cur, ast.Name):
        return cur.id
    return None
