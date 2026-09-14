"""An honest, minimal NodeTypeProvider: one `module` observation per Python
file. Its struct signature is the file's AST without docstrings, so a file
keeps its identity across every corpus mutation that keeps its path (the
qualname layer matches first) — which is exactly what it declares."""
from __future__ import annotations

import ast
import hashlib
import os

from ..graph_api import MUTATIONS, NodeObservation, Signature


def _strip_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                node.body = b[1:] or [ast.Pass()]


def module_name(rel_path: str) -> str:
    p = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    return p.replace("\\", "/").replace("/", ".")


class ModuleProvider:
    type_id = "provledger.module_example"
    schema_version = 1
    requires: tuple[str, ...] = ()

    def extract(self, ctx) -> list[NodeObservation]:
        out = []
        for rel in sorted(ctx.file_map):
            if not rel.endswith(".py"):
                continue
            try:
                src = open(os.path.join(ctx.repo_root, rel), encoding="utf-8").read()
                tree = ast.parse(src)
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            _strip_docstrings(tree)
            n_defs = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for n in tree.body)
            sig = hashlib.sha1(ast.dump(tree).encode()).hexdigest()[:16]
            out.append(NodeObservation(
                type_id=self.type_id, node_type="module", qualified_name=module_name(rel), file_path=rel,
                line_start=1, line_end=len(src.splitlines()) or 1,
                signatures=(Signature("qualname", module_name(rel)), Signature("struct", sig),
                            Signature("dataflow", None, trivial=True)),
                attrs={"n_defs": n_defs}))
        return out

    def attributes_schema(self):
        return {"required": ["n_defs"], "types": {"n_defs": "int"}}

    def declared_stability(self) -> dict[str, str]:
        return {m: "preserved" for m in MUTATIONS}
