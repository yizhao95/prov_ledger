"""Python AST analysis: function/class/method nodes + defines/imports/calls edges."""
from __future__ import annotations

import ast
import os
import sqlite3
from typing import Dict

from . import _resolve, store


def _qual(module: str, *parts: str) -> str:
    return ".".join([module, *parts])


def _module_name(rel_path: str) -> str:
    no_ext = os.path.splitext(rel_path)[0]
    return no_ext.replace(os.sep, ".")


def imported_symbols(source: str) -> set[str]:
    """Return the set of names a module's imports BIND into local scope.

    - `import os`            -> 'os'  (top-level package name)
    - `import os.path`       -> 'os'
    - `import numpy as np`   -> 'np'
    - `from m import a, b`   -> 'a', 'b'
    - `from m import x as y` -> 'y'  (asname wins; 'x' is NOT bound)

    Used by the unresolved-symbol resolver to know which bare names are
    legitimately bound via imports. Best-effort: unparseable source -> empty set.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names.add(alias.asname)
                else:
                    names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name)
    return names


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    """Populate function/class/method nodes and defines/imports/calls edges.

    `file_map` maps relative file_path -> file node id (from walker.walk).
    """
    types = {
        "function": store.get_or_create_node_type(conn, "function"),
        "class": store.get_or_create_node_type(conn, "class"),
        "method": store.get_or_create_node_type(conn, "method"),
        "module": store.get_or_create_node_type(conn, "module"),
    }
    edges = {
        "defines": store.get_or_create_edge_type(conn, "defines"),
        "imports": store.get_or_create_edge_type(conn, "imports"),
        "calls": store.get_or_create_edge_type(conn, "calls"),
    }

    repo_root = os.path.abspath(repo_root)

    # Phase 1: create function/class/method nodes + defines/imports edges for
    # ALL files first (no call edges yet). Collect @property method names globally.
    property_names: set[str] = set()
    for rel_path, file_node in file_map.items():
        if not rel_path.endswith(".py"):
            continue
        abs_path = os.path.join(repo_root, rel_path)
        try:
            with open(abs_path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        module = _module_name(rel_path)
        v = _ModuleVisitor(conn, rel_path, module, file_node, types, edges)
        v.run(tree)
        property_names |= v.property_names

    # Phase 2: resolve call edges GLOBALLY now that every node exists, so
    # cross-module calls resolve (PSG-C2/#8) and same-named calls bind correctly.
    idx = _resolve.build_index(conn)
    for rel_path in file_map:
        if not rel_path.endswith(".py"):
            continue
        abs_path = os.path.join(repo_root, rel_path)
        try:
            with open(abs_path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        _collect_calls_global(conn, tree, _module_name(rel_path), idx,
                              edges["calls"], property_names)


class _ModuleVisitor:
    def __init__(self, conn, rel_path, module, file_node, types, edges):
        self.conn = conn
        self.rel_path = rel_path
        self.module = module
        self.file_node = file_node
        self.types = types
        self.edges = edges
        # qualified_name -> node id for callables defined in this module
        self.callables: Dict[str, int] = {}
        # simple name -> node id (last wins) for resolving call targets
        self.by_name: Dict[str, int] = {}
        # simple name -> count of definitions (>1 => ambiguous resolution)
        self.name_counts: Dict[str, int] = {}
        # method names decorated with @property (read-as-call detection)
        self.property_names: set[str] = set()

    def run(self, tree: ast.Module) -> None:
        # First pass: imports + define all callables (so calls can resolve).
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._add_import(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self._add_import(node.module.split(".")[0])

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._add_function(node)
            elif isinstance(node, ast.ClassDef):
                self._add_class(node)
        # Call edges are resolved in a SECOND GLOBAL pass (see analyze /
        # _collect_calls_global) so cross-module calls can resolve.

    def _add_import(self, target: str) -> None:
        mod_node = store.add_node(
            self.conn, self.types["module"], name=target, qualified_name=target,
        )
        store.add_edge(self.conn, self.edges["imports"], self.file_node, mod_node,
                       metadata={"module": target})

    def _add_function(self, node) -> int:
        qn = _qual(self.module, node.name)
        nid = store.add_node(
            self.conn, self.types["function"], name=node.name, qualified_name=qn,
            file_path=self.rel_path, line_start=node.lineno,
            line_end=getattr(node, "end_lineno", node.lineno),
        )
        store.add_edge(self.conn, self.edges["defines"], self.file_node, nid)
        self.callables[qn] = nid
        self.by_name[node.name] = nid
        self.name_counts[node.name] = self.name_counts.get(node.name, 0) + 1
        return nid

    def _add_class(self, node) -> int:
        qn = _qual(self.module, node.name)
        cid = store.add_node(
            self.conn, self.types["class"], name=node.name, qualified_name=qn,
            file_path=self.rel_path, line_start=node.lineno,
            line_end=getattr(node, "end_lineno", node.lineno),
        )
        store.add_edge(self.conn, self.edges["defines"], self.file_node, cid)
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mqn = _qual(self.module, node.name, item.name)
                mid = store.add_node(
                    self.conn, self.types["method"], name=item.name,
                    qualified_name=mqn, file_path=self.rel_path,
                    line_start=item.lineno,
                    line_end=getattr(item, "end_lineno", item.lineno),
                )
                store.add_edge(self.conn, self.edges["defines"], cid, mid)
                self.callables[mqn] = mid
                self.by_name[item.name] = mid
                self.name_counts[item.name] = self.name_counts.get(item.name, 0) + 1
                if _is_property(item):
                    self.property_names.add(item.name)
        return cid

    def _collect_calls(self, fn_node, src_id: int) -> None:
        for sub in ast.walk(fn_node):
            if isinstance(sub, ast.Call):
                name = self._call_name(sub.func)
                if name and name in self.by_name:
                    dst = self.by_name[name]
                    if dst != src_id:
                        confidence = (
                            "high" if self.name_counts.get(name, 0) <= 1
                            else "inferred"
                        )
                        store.add_edge(self.conn, self.edges["calls"], src_id, dst,
                                       metadata={"line": sub.lineno,
                                                 "confidence": confidence})
            elif isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Load):
                # `obj.prop` read of a known @property counts as a call, so
                # properties don't appear 'never called'. Skip if it's the
                # callee of a Call (handled above) to avoid double counting.
                name = sub.attr
                if name in self.property_names and name in self.by_name:
                    dst = self.by_name[name]
                    if dst != src_id:
                        store.add_edge(self.conn, self.edges["calls"], src_id, dst,
                                       metadata={"line": sub.lineno,
                                                 "confidence": "inferred",
                                                 "via": "property"})

    @staticmethod
    def _call_name(func) -> str | None:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None


def _collect_calls_global(conn, tree, module, idx, calls_e, property_names) -> None:
    """Resolve `calls` edges for one module against the GLOBAL index (PSG-C2/#8).

    Name calls (`foo()`) resolve module-qualified first, then cross-module unique,
    then per-candidate (inferred). Attribute calls (`obj.foo()`) resolve by SIMPLE
    NAME only (no `module.name` top-level fast-path) so `self.foo()` is never bound
    to a same-named top-level function at high confidence (PSG-#4). Ambiguity emits
    one inferred edge per candidate; (src, dst) is de-duplicated per function.
    """
    for fn, qual in _resolve.iter_funcs(tree, module):
        src_id = idx.by_qual.get(qual)
        if src_id is None:
            continue
        # Enclosing class (if this is a method): qual is module.Class.method.
        tail = qual[len(module) + 1:]
        klass = tail.split(".")[0] if "." in tail else None
        seen: set[int] = set()
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Call):
                name = _resolve.call_name(sub.func)
                if not name:
                    continue
                func = sub.func
                if isinstance(func, ast.Attribute):
                    recv = func.value
                    if isinstance(recv, ast.Name) and recv.id == "self" and klass:
                        # self.foo() -> THIS class's method (PSG-#4), not a
                        # same-named top-level function.
                        cands = idx.resolve(name, module=module, klass=klass)
                    else:
                        cands = idx.resolve(name)  # other attribute: simple-name only
                else:
                    cands = idx.resolve(name, module=module)
                _emit_calls(conn, calls_e, src_id, cands, seen,
                            line=getattr(sub, "lineno", None))
            elif isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Load):
                # `obj.prop` read of a known @property counts as a call.
                if sub.attr in property_names:
                    _emit_calls(conn, calls_e, src_id, idx.resolve(sub.attr), seen,
                                line=getattr(sub, "lineno", None), via="property")


def _emit_calls(conn, calls_e, src_id, cands, seen, line=None, via=None) -> None:
    for dst_id, conf in cands:
        if dst_id == src_id or dst_id in seen:
            continue
        seen.add(dst_id)
        meta = {"line": line, "confidence": conf}
        if via:
            meta["via"] = via
        store.add_edge(conn, calls_e, src_id, dst_id, metadata=meta)


def _is_property(node) -> bool:
    """True if a function/method def is decorated with @property (or @cached_property)."""
    for dec in getattr(node, "decorator_list", []):
        name = None
        if isinstance(dec, ast.Name):
            name = dec.id
        elif isinstance(dec, ast.Attribute):
            name = dec.attr
        if name in ("property", "cached_property"):
            return True
    return False
