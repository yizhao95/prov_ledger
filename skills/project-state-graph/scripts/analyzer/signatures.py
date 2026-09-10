"""Deterministic identity signatures (spec §2.3). Pure functions.

struct_sig(node)      — what a function/class DOES, independent of what it is
                        called, how it is formatted, what its locals are named
                        and what its annotations say. sha1[:16] of the
                        alpha-normalised, annotation-free, docstring-free AST.
dataflow_sig(conn,id) — what flows through it, computed from the edges the
                        analyzers already recorded (feeds / produces / reads_* /
                        writes_* / calls). Trivial (and excluded from matching)
                        when nothing is known.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json

_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def _params(fn) -> list[ast.arg]:
    a = fn.args
    return a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]


class _Alpha(ast.NodeTransformer):
    """Rename params/locals to _v<i>; leaves every other Name (globals, call
    targets, attributes) untouched — those are part of what the code does."""

    def __init__(self, mapping: dict[str, str]):
        self.m = mapping

    def visit_Name(self, node):
        if node.id in self.m:
            return ast.copy_location(ast.Name(id=self.m[node.id], ctx=node.ctx), node)
        return node

    def visit_arg(self, node):
        if node.arg in self.m:
            node.arg = self.m[node.arg]
        return node


def _locals(node) -> list[str]:
    """Params of this def AND of every nested def (so a class node normalises its
    methods' params too) plus Store-context Names, in first-occurrence order."""
    names: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, _DEF_TYPES):
            for p in _params(n):
                if p.arg not in names:
                    names.append(p.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id not in names:
            names.append(n.id)
    return names


def _strip_annotations(node) -> None:
    """Type names follow class renames; identity must not. dtype information is
    the dataflow signature's job."""
    for n in ast.walk(node):
        if isinstance(n, _DEF_TYPES):
            n.returns = None
            for p in _params(n):
                p.annotation = None
        elif isinstance(n, ast.AnnAssign):
            n.annotation = ast.Constant(value=None)


def _strip_docstring(node) -> None:
    b = node.body
    if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
            and isinstance(b[0].value.value, str):
        node.body = b[1:] or [ast.Pass()]


def struct_sig(fn: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    node = copy.deepcopy(fn)
    _strip_docstring(node)
    for n in ast.walk(node):            # nested defs / methods lose theirs too
        if isinstance(n, _DEF_TYPES + (ast.ClassDef,)) and n is not node:
            _strip_docstring(n)
    _strip_annotations(node)
    mapping = {name: f"_v{i}" for i, name in enumerate(_locals(node))}
    node = _Alpha(mapping).visit(node)
    node.name = "_fn"
    return _h(ast.dump(node, include_attributes=False))


def dataflow_sig(conn, node_id: int) -> tuple[str, bool, dict]:
    """(sig, trivial, attrs) from the graph's edges around `node_id`.

    attrs = {params: [dtype per feeds edge], return: dtype of produces,
             reads: sorted reads_sql/reads_api targets, writes: ..., callees:
             sorted high-confidence calls targets}. trivial when every part is
             empty/unknown — such a signature never participates in matching.
    """
    def targets(edge: str) -> list[str]:
        return sorted(r[0] for r in conn.execute(
            """SELECT n.name FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
               JOIN node n ON n.id=e.dst_node_id
               WHERE t.name=? AND e.src_node_id=?
                 AND (e.confidence IS NULL OR e.confidence='high')""", (edge, node_id)))

    params = [r[0] or "unknown" for r in conn.execute(
        """SELECT n.dtype FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node n ON n.id=e.src_node_id
           WHERE t.name='feeds' AND e.dst_node_id=? ORDER BY n.id""", (node_id,))]
    ret = conn.execute(
        """SELECT n.dtype FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node n ON n.id=e.dst_node_id
           WHERE t.name='produces' AND e.src_node_id=? ORDER BY n.id LIMIT 1""", (node_id,)).fetchone()
    attrs = {
        "params": params,
        "return": ret[0] if ret and ret[0] else "unknown",
        "reads": targets("reads_sql") + targets("reads_api"),
        "writes": targets("writes_sql") + targets("writes_api"),
        "callees": targets("calls"),
    }
    trivial = (all(p == "unknown" for p in params) and attrs["return"] == "unknown"
               and not attrs["reads"] and not attrs["writes"] and not attrs["callees"])
    return _h(json.dumps(attrs, sort_keys=True)), trivial, attrs
