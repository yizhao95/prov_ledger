"""A provider that gets its identity rule WRONG on purpose (test fixture for
the conformance suite): its struct signature α-normalises locals but mixes
the function NAME in, so a renamed function becomes a different node — yet it
declares rename_function as preserved. Everything else about it is honest."""
from __future__ import annotations

import ast
import hashlib
import os

from ..graph_api import MUTATIONS, NodeObservation, Signature
from .example_provider import _strip_docstrings, module_name


class _Alpha(ast.NodeTransformer):
    """Rename args and locally bound names to positional tokens (a lightweight
    version of the real struct normalisation)."""

    def __init__(self):
        self.map: dict[str, str] = {}

    def _tok(self, name: str) -> str:
        return self.map.setdefault(name, f"_v{len(self.map)}")

    def visit_arg(self, node):
        node.arg = self._tok(node.arg)
        return node

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store) or node.id in self.map:
            node.id = self._tok(node.id)
        return node


def _struct(node: ast.AST, include_name: bool) -> str:
    _strip_docstrings(node)
    norm = _Alpha().visit(node)
    text = ast.dump(norm)
    if include_name:
        text = f"{getattr(node, 'name', '')}|{text}"
    return hashlib.sha1(text.encode()).hexdigest()[:16]


class LiarProvider:
    type_id = "acme.liar"
    schema_version = 1
    requires: tuple[str, ...] = ()
    include_name = True            # the lie's mechanism

    def extract(self, ctx) -> list[NodeObservation]:
        out = []
        for rel in sorted(ctx.file_map):
            if not rel.endswith(".py"):
                continue
            try:
                tree = ast.parse(open(os.path.join(ctx.repo_root, rel), encoding="utf-8").read())
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            mod = module_name(rel)
            defs = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defs.append(("function", f"{mod}.{node.name}", node))
                elif isinstance(node, ast.ClassDef):
                    defs.append(("class", f"{mod}.{node.name}", node))
                    for sub in node.body:              # methods too, so class-based cases are exercised
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            defs.append(("method", f"{mod}.{node.name}.{sub.name}", sub))
            for kind, qn, node in defs:
                out.append(NodeObservation(
                    type_id=self.type_id, node_type=kind, qualified_name=qn, file_path=rel,
                    line_start=node.lineno, line_end=getattr(node, "end_lineno", node.lineno),
                    signatures=(Signature("qualname", qn), Signature("struct", _struct(node, self.include_name)),
                                Signature("dataflow", None, trivial=True)),
                    attrs={"kind": kind}))
        return out

    def attributes_schema(self):
        return {"required": ["kind"], "types": {"kind": "str"}}

    def declared_stability(self) -> dict[str, str]:
        d = {m: "preserved" for m in MUTATIONS}
        d["delete_function"] = "broken"
        d["swap_two_similar"] = "broken"       # names inside the struct: no ambiguity, plain removal
        d["rename_function"] = "preserved"     # THE LIE — the name is in the struct signature
        return d
