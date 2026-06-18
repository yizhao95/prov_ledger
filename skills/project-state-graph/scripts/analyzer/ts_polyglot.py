"""Polyglot tree-sitter extractor: JS / HTML / CSS -> code graph.

Builds on the generic graph store (analyzer.store) and the file nodes
discovered by analyzer.walker. Public entry point: ``analyze``.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Dict, Optional

import tree_sitter_css
import tree_sitter_html
import tree_sitter_javascript
from tree_sitter import Language, Parser

from . import store

_JS_LANG = Language(tree_sitter_javascript.language())
_HTML_LANG = Language(tree_sitter_html.language())
_CSS_LANG = Language(tree_sitter_css.language())


def _parser(lang: Language) -> Parser:
    return Parser(lang)


def _read(rel_path: str, repo_root: str) -> Optional[str]:
    abs_path = os.path.join(repo_root, rel_path)
    try:
        with open(abs_path, "r", encoding="utf8") as fh:
            return fh.read()
    except OSError:
        return None


def _child_by_type(node, type_name):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


# --------------------------------------------------------------------------- #
# JavaScript
# --------------------------------------------------------------------------- #
def _analyze_js(conn, repo_root, rel_path, func_type_id, calls_type_id, func_index):
    """Pass 1: collect function nodes for one JS file (mutates func_index)."""
    source = _read(rel_path, repo_root)
    if source is None:
        return []
    tree = _parser(_JS_LANG).parse(bytes(source, "utf8"))

    local_funcs = []  # (name, ts_node_of_body_scope)

    def visit(node):
        if node.type == "function_declaration":
            name_node = _child_by_type(node, "identifier")
            if name_node is not None:
                _register_func(name_node.text.decode("utf8"), node)
        elif node.type == "variable_declarator":
            value = node.children[-1] if node.children else None
            if value is not None and value.type in (
                "arrow_function",
                "function_expression",
            ):
                name_node = _child_by_type(node, "identifier")
                if name_node is not None:
                    _register_func(name_node.text.decode("utf8"), node)
                    return  # body handled via register; keep walking children
        for c in node.children:
            visit(c)

    def _register_func(name, ts_node):
        node_id = store.add_node(
            conn,
            func_type_id,
            name=name,
            qualified_name=f"{rel_path}::{name}",
            file_path=rel_path,
            line_start=ts_node.start_point[0] + 1,
            line_end=ts_node.end_point[0] + 1,
        )
        func_index[name] = node_id
        local_funcs.append((name, ts_node))

    visit(tree.root_node)
    return local_funcs


def _extract_js_calls(conn, calls_type_id, local_funcs, func_index):
    """Pass 2: create calls edges now that all function nodes exist."""
    for caller_name, ts_node in local_funcs:
        caller_id = func_index.get(caller_name)
        if caller_id is None:
            continue
        callees = set()
        _collect_calls(ts_node, callees, skip_root=True)
        for callee in callees:
            callee_id = func_index.get(callee)
            if callee_id is not None:
                store.add_edge(conn, calls_type_id, caller_id, callee_id)


def _collect_calls(node, out, skip_root=False):
    if not skip_root and node.type == "call_expression":
        fn = node.children[0] if node.children else None
        if fn is not None and fn.type == "identifier":
            out.add(fn.text.decode("utf8"))
    for c in node.children:
        _collect_calls(c, out)


# --------------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------------- #
def _selector_names(node, out):
    if node.type == "class_selector":
        name_node = _child_by_type(node, "class_name")
        if name_node is not None:
            out.add("." + name_node.text.decode("utf8"))
    elif node.type == "id_selector":
        name_node = _child_by_type(node, "id_name")
        if name_node is not None:
            out.add("#" + name_node.text.decode("utf8"))
    for c in node.children:
        _selector_names(c, out)


def _get_or_create_selector(conn, sel_type_id, name, selector_index, rel_path=None):
    if name in selector_index:
        return selector_index[name]
    node_id = store.add_node(
        conn, sel_type_id, name=name, qualified_name=name, file_path=rel_path
    )
    selector_index[name] = node_id
    return node_id


def _analyze_css(conn, repo_root, rel_path, sel_type_id, selector_index):
    source = _read(rel_path, repo_root)
    if source is None:
        return
    tree = _parser(_CSS_LANG).parse(bytes(source, "utf8"))
    names = set()
    _selector_names(tree.root_node, names)
    for name in names:
        _get_or_create_selector(conn, sel_type_id, name, selector_index, rel_path)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def _attr_value(tag_node, attr_name):
    for attr in tag_node.children:
        if attr.type != "attribute":
            continue
        name_node = _child_by_type(attr, "attribute_name")
        if name_node is None or name_node.text.decode("utf8") != attr_name:
            continue
        qval = _child_by_type(attr, "quoted_attribute_value")
        if qval is not None:
            inner = _child_by_type(qval, "attribute_value")
            if inner is not None:
                return inner.text.decode("utf8")
        raw = _child_by_type(attr, "attribute_value")
        if raw is not None:
            return raw.text.decode("utf8")
    return None


def _analyze_html(
    conn,
    repo_root,
    rel_path,
    html_file_id,
    references_type_id,
    sel_type_id,
    selector_index,
    basename_to_file_id,
):
    source = _read(rel_path, repo_root)
    if source is None:
        return
    tree = _parser(_HTML_LANG).parse(bytes(source, "utf8"))

    def visit(node):
        if node.type in ("element", "script_element"):
            start = _child_by_type(node, "start_tag")
            if start is not None:
                tag_name_node = _child_by_type(start, "tag_name")
                tag = tag_name_node.text.decode("utf8") if tag_name_node else ""
                # file references: <link href=...>, <script src=...>
                ref_target = None
                if tag == "link":
                    ref_target = _attr_value(start, "href")
                elif tag == "script":
                    ref_target = _attr_value(start, "src")
                if ref_target:
                    target_id = basename_to_file_id.get(os.path.basename(ref_target))
                    if target_id is not None:
                        store.add_edge(
                            conn, references_type_id, html_file_id, target_id
                        )
                # css selector usage: class / id attributes
                class_val = _attr_value(start, "class")
                if class_val:
                    for cls in class_val.split():
                        sid = _get_or_create_selector(
                            conn, sel_type_id, "." + cls, selector_index
                        )
                        store.add_edge(conn, references_type_id, html_file_id, sid)
                id_val = _attr_value(start, "id")
                if id_val:
                    sid = _get_or_create_selector(
                        conn, sel_type_id, "#" + id_val, selector_index
                    )
                    store.add_edge(conn, references_type_id, html_file_id, sid)
        for c in node.children:
            visit(c)

    visit(tree.root_node)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def analyze(conn: sqlite3.Connection, repo_root: str, file_map: Dict[str, int]) -> None:
    """Extract a JS/HTML/CSS code graph into the store.

    ``file_map`` maps relative file paths -> file node ids (from walker.walk).
    """
    func_type_id = store.get_or_create_node_type(conn, "function")
    sel_type_id = store.get_or_create_node_type(conn, "css_selector")
    calls_type_id = store.get_or_create_edge_type(conn, "calls")
    references_type_id = store.get_or_create_edge_type(conn, "references")

    basename_to_file_id = {
        os.path.basename(rel): fid for rel, fid in file_map.items()
    }

    func_index: Dict[str, int] = {}
    selector_index: Dict[str, int] = {}

    # JS pass 1: collect functions across all files first so calls resolve.
    js_locals = []
    for rel_path in file_map:
        if rel_path.lower().endswith((".js", ".mjs")):
            local = _analyze_js(
                conn, repo_root, rel_path, func_type_id, calls_type_id, func_index
            )
            js_locals.append(local)

    # JS pass 2: calls edges.
    for local in js_locals:
        _extract_js_calls(conn, calls_type_id, local, func_index)

    # CSS: selector nodes (so HTML references can dedupe against them).
    for rel_path in file_map:
        if rel_path.lower().endswith(".css"):
            _analyze_css(conn, repo_root, rel_path, sel_type_id, selector_index)

    # HTML: file references + selector usage.
    for rel_path, file_id in file_map.items():
        if rel_path.lower().endswith((".html", ".htm")):
            _analyze_html(
                conn,
                repo_root,
                rel_path,
                file_id,
                references_type_id,
                sel_type_id,
                selector_index,
                basename_to_file_id,
            )
