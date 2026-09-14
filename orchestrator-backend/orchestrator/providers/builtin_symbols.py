"""Reference NodeTypeProvider for function / method / class identity (and the
name-only data sources) — the built-in symbol layer, written against the same
public API a third party gets (phase 6 Task 3).

The identity signatures live here (moved from the analyzer's signatures.py):
struct_sig(node)      — what a function/class DOES: sha1[:16] of the
                        alpha-normalised, annotation-free, docstring-free AST.
dataflow_sig(conn,id) — what flows through it, from the graph's edges
                        (feeds / produces / reads_* / writes_* / calls);
                        trivial (never matched) when nothing is known.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os

from ..graph_api import MUTATIONS, NodeObservation, Signature

SYMBOL_TYPES = ("function", "method", "class")
NAME_ONLY_TYPES = ("sql_table", "bq_dataset", "api_source", "dataset")

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


# ── source lookup (moved from the analyzer's history.py) ─────────────────────
def _defs_of(tree: ast.Module) -> list[ast.AST]:
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def _find_def(defs: list[ast.AST], line_start: int | None, name: str | None):
    """The def recorded by py_ast: same start line, else the unique def of that
    name (decorators can shift the recorded line by one or two)."""
    if line_start is not None:
        for d in defs:
            if d.lineno == line_start:
                return d
    if name:
        named = [d for d in defs if d.name == name]
        if len(named) == 1:
            return named[0]
        if line_start is not None and named:
            return min(named, key=lambda d: abs(d.lineno - line_start))
    return None


class BuiltinSymbolProvider:
    """function / method / class (+ name-only data sources): qualname, struct
    and dataflow signatures. Emits in the host's historical order: per type
    (SYMBOL_TYPES then NAME_ONLY_TYPES), sorted by (qualified name or name, id)."""
    type_id = "provledger.symbol"
    schema_version = 1
    requires: tuple[str, ...] = ()

    def extract(self, ctx) -> list[NodeObservation]:
        rows = ctx.node_rows(SYMBOL_TYPES + NAME_ONLY_TYPES)
        by_type: dict[str, list] = {}
        for r in rows:
            by_type.setdefault(r[1], []).append(tuple(r))
        trees: dict[str, list[ast.AST]] = {}
        out: list[NodeObservation] = []
        for ntype in SYMBOL_TYPES + NAME_ONLY_TYPES:
            for nid, _t, qn, name, path, ls, le, _dtype in sorted(by_type.get(ntype, []), key=lambda r: (r[2] or r[3], r[0])):
                struct = df_sig = None
                trivial, attrs = True, {}
                if ntype in SYMBOL_TYPES and path:
                    if path not in trees:
                        try:
                            src = open(os.path.join(ctx.repo_root, path), encoding="utf-8").read()
                            trees[path] = _defs_of(ast.parse(src))
                        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
                            trees[path] = []
                    node = _find_def(trees[path], ls, name)
                    if node is not None:
                        struct = struct_sig(node)
                    df_sig, trivial, attrs = dataflow_sig(ctx.conn_ro, nid)
                out.append(NodeObservation(
                    type_id=self.type_id, node_type=ntype, qualified_name=qn or name, file_path=path,
                    line_start=ls, line_end=le,
                    signatures=(Signature("qualname", qn or name), Signature("struct", struct),
                                Signature("dataflow", df_sig, trivial=bool(trivial))),
                    attrs=dict(attrs), node_id=nid))
        return out

    def attributes_schema(self):
        return {"types": {"params": "list", "return": "str", "reads": "list", "writes": "list", "callees": "list"}}

    def declared_stability(self) -> dict[str, str]:
        d = {m: "preserved" for m in MUTATIONS}
        d["delete_function"] = "broken"
        d["swap_two_similar"] = "ambiguous"
        return d
