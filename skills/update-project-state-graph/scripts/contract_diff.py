"""contract_diff.py — the provLedger Phase A contract-drift engine.

A shared base-vs-head fingerprint-and-compare engine for the
update-project-state-graph reviewer. For a resolved diff range it pulls each
touched file's BOTH versions via `git show base:path` / `git show head:path`,
parses the FULL AST of each version (never regex on diff text — regex breaks on
default args, type annotations, multi-line signatures, and decorators), extracts
structured fingerprints, and compares them.

It applies the same engine to three contract types:

  A-1  Python signature contract     — signature_contract(...)
  A-2  DataFrame column-schema       — dataframe_schema_contract(...)
  A-3  SQL query projection contract — sql_contract(...)

This file (step C) contains the SHARED ENGINE. The three verdict functions are
appended in later steps (E / G / I).

Stdlib only: ast, subprocess, sqlite3, json.
"""
from __future__ import annotations

import ast
import subprocess
from typing import Optional


# ── git plumbing ─────────────────────────────────────────────────────────────────

def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    )


def git_show(repo: str, ref: str, path: str) -> str:
    """Return the text of `path` at `ref` (e.g. `git show base:path`).

    Missing file (added on one side, or absent at that ref) -> empty string.
    """
    proc = _git(repo, "show", f"{ref}:{path}")
    if proc.returncode != 0:
        return ""
    return proc.stdout


def changed_files(repo: str, base: str, head: str) -> list[str]:
    """Repo-relative paths changed between base and head (any status)."""
    proc = _git(repo, "diff", "--name-only", f"{base}..{head}")
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


# ── Python signature fingerprinting ──────────────────────────────────────────────

def _annotation_str(node: Optional[ast.AST]) -> Optional[str]:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - ast.unparse is robust on valid nodes
        return None


def _fingerprint_function(node: ast.FunctionDef | ast.AsyncFunctionDef,
                          qualified_name: str) -> dict:
    a = node.args
    positional = [p.arg for p in (a.posonlyargs + a.args)]
    kwonly = [p.arg for p in a.kwonlyargs]
    annotations: dict[str, Optional[str]] = {}
    for p in (a.posonlyargs + a.args + a.kwonlyargs):
        if p.annotation is not None:
            annotations[p.arg] = _annotation_str(p.annotation)
    return {
        "qualified_name": qualified_name,
        "positional": positional,
        "kwonly": kwonly,
        "star_args": a.vararg is not None,
        "kwstar": a.kwarg is not None,
        "annotations": annotations,
        "returns": _annotation_str(node.returns),
    }


def extract_python_signatures(source: str) -> dict[str, dict]:
    """Parse `source` and return {qualified_name: fingerprint}.

    The fingerprint is signature-layer only — the function body is ignored, so a
    body-only change yields an identical fingerprint. Methods are qualified by
    their class (e.g. ``C.m``). Malformed source returns ``{}`` (never raises).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}

    out: dict[str, dict] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = f"{prefix}{child.name}"
                out[qname] = _fingerprint_function(child, qname)
                # nested functions/classes qualified under this function
                visit(child, f"{qname}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")

    visit(tree, "")
    return out


def compare_signatures(base: dict[str, dict], head: dict[str, dict]) -> list[dict]:
    """Compare two fingerprint maps (same-name functions only).

    Returns [{qualified_name, change_kind}] where change_kind is one of:
      - 'signature_changed'        : positional/kwonly/star/annotation params differ
      - 'return_contract_changed'  : return annotation differs

    Functions present on only one side are NOT reported (rename is handled by the
    reviewer's changed_symbols path). Body-only changes produce no entry.
    """
    changes: list[dict] = []
    for qname in sorted(set(base) & set(head)):
        b, h = base[qname], head[qname]
        sig_differs = (
            b["positional"] != h["positional"]
            or b["kwonly"] != h["kwonly"]
            or b["star_args"] != h["star_args"]
            or b["kwstar"] != h["kwstar"]
            or b["annotations"] != h["annotations"]
        )
        if sig_differs:
            changes.append({"qualified_name": qname,
                            "change_kind": "signature_changed"})
        if b["returns"] != h["returns"]:
            changes.append({"qualified_name": qname,
                            "change_kind": "return_contract_changed"})
    return changes


# ── shared graph helpers ─────────────────────────────────────────────────────────

import json
import sqlite3


def _module_from_path(path: str) -> str:
    """'pkg/a.py' -> 'pkg.a' (matches the graph's module-qualified names)."""
    p = path[:-3] if path.endswith(".py") else path
    return p.replace("\\", "/").replace("/", ".")


def _card_for(conn: sqlite3.Connection, qualified_name: str,
              module: Optional[str] = None) -> Optional[dict]:
    """Consistency card for a function (PSG-C5).

    Resolve by MODULE-qualified name first (extract_python_signatures emits
    `helper` / `C.m` without a module, so we prepend the changed file's module);
    fall back to a bare-name match ONLY when it is UNIQUE. An ambiguous bare name
    returns None — never an arbitrary wrong card (which produced false PASSes).
    """
    for q in ([f"{module}.{qualified_name}"] if module else []) + [qualified_name]:
        row = conn.execute(
            """SELECT cc.card_json FROM consistency_card cc
               JOIN node n ON n.id = cc.symbol_id WHERE n.qualified_name = ?""",
            (q,),
        ).fetchone()
        if row:
            return json.loads(row[0]) if row[0] else None
    bare = qualified_name.split(".")[-1]
    rows = conn.execute(
        """SELECT cc.card_json FROM consistency_card cc
           JOIN node n ON n.id = cc.symbol_id WHERE n.name = ?""",
        (bare,),
    ).fetchall()
    if len(rows) == 1:
        return json.loads(rows[0][0]) if rows[0][0] else None
    return None  # ambiguous or absent — do not guess


def _file_of(conn: sqlite3.Connection, name: str,
             module: Optional[str] = None) -> Optional[str]:
    """Resolve a symbol to its file_path (PSG-C5): qualified first, then UNIQUE
    bare name only (no silent LIMIT 1 over ambiguous candidates)."""
    for q in ([f"{module}.{name}"] if module else []) + [name]:
        row = conn.execute(
            "SELECT file_path FROM node WHERE qualified_name = ?", (q,)
        ).fetchone()
        if row:
            return row[0]
    rows = conn.execute(
        "SELECT file_path FROM node WHERE name = ?", (name.split(".")[-1],)
    ).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def _verdict(gaps: list[dict], clean_text: str, header: str) -> dict:
    """Assemble {ok, gaps, text}. ok is False iff any gap has severity 'fail'."""
    if not gaps:
        return {"ok": True, "gaps": [], "text": clean_text}
    ok = not any(g.get("severity") == "fail" for g in gaps)
    lines = [header]
    for g in gaps:
        lines.append(f"  - [{g.get('severity')}] {g.get('detail', '')}")
    return {"ok": ok, "gaps": gaps, "text": "\n".join(lines)}


# ── A-1 · Python signature verdict ───────────────────────────────────────────────

def signature_contract(db_path: str, repo: str, base: str, head: str) -> dict:
    """Verdict for changed Python signatures against the deep graph.

    For each function whose signature/return-contract changed between base/head:
      - pull its consistency_card (callers + output_consumers),
      - resolve each dependent's file_path from the graph,
      - if a dependent's file is NOT in the diff's changed-files set -> FAIL,
        else -> warning (assume handled; still surfaced to confirm).

    Returns {ok, gaps:[{kind, qualified_name, caller, severity, detail}], text}.
    ok is False iff any gap is severity 'fail'.
    """
    files = set(changed_files(repo, base, head))
    py_files = [f for f in files if f.endswith(".py")]

    changes: list[dict] = []
    for path in py_files:
        base_fp = extract_python_signatures(git_show(repo, base, path))
        head_fp = extract_python_signatures(git_show(repo, head, path))
        module = _module_from_path(path)  # PSG-C5: qualify changed symbols by module
        for ch in compare_signatures(base_fp, head_fp):
            ch["module"] = module
            changes.append(ch)

    if not changes:
        return {"ok": True, "gaps": [],
                "text": "No signature/return-contract changes detected."}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    gaps: list[dict] = []
    try:
        for ch in changes:
            qname = ch["qualified_name"]
            card = _card_for(conn, qname, module=ch.get("module"))
            if card is None:
                continue  # changed fn not in graph (new code) — nothing downstream
            dependents = list(card.get("callers", []))
            if ch["change_kind"] == "return_contract_changed":
                dependents += list(card.get("output_consumers", []))
            for dep in sorted(set(dependents)):
                dep_file = _file_of(conn, dep)
                in_diff = dep_file in files if dep_file else False
                severity = "warning" if in_diff else "fail"
                gaps.append({
                    "kind": ch["change_kind"],
                    "qualified_name": qname,
                    "caller": dep,
                    "severity": severity,
                    "detail": (f"{qname} {ch['change_kind']}, dependent '{dep}' "
                               f"({dep_file or '?'}) "
                               + ("was updated in this diff (confirm)"
                                  if in_diff else "was NOT updated")),
                })
    finally:
        conn.close()

    return _verdict(
        gaps,
        clean_text="No signature gaps — all dependents updated.",
        header="Signature contract changes (callers/consumers may be stale):",
    )


# ── A-2 · DataFrame column-schema verdict ────────────────────────────────────────

def dataframe_schema_contract(db_path: str, fn_qname: str, *,
                              base_cols: dict, head_cols: dict,
                              changed_files: set) -> dict:
    """Verdict for a DataFrame-producing function's column-schema drift.

    A DataFrame-producing function's "signature" is its column-set + per-column
    dtype. base_cols / head_cols are {column_name: dtype} fingerprints (sourced
    from the dataframe/column/has_column nodes at each side). A dropped, renamed
    (= dropped old name), or retyped column whose output_consumers were NOT
    updated in this diff is a FAIL — same severity as a stale reference.

    Returns {ok, gaps:[{kind, column, consumer, severity, detail}], text}.
    """
    dropped = [c for c in base_cols if c not in head_cols]
    retyped = [c for c in base_cols
               if c in head_cols and base_cols[c] != head_cols[c]]
    if not dropped and not retyped:
        return {"ok": True, "gaps": [],
                "text": "No DataFrame column-schema changes detected."}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    gaps: list[dict] = []
    try:
        card = _card_for(conn, fn_qname) or {}
        consumers = sorted(set(card.get("output_consumers", [])))

        def _emit(kind: str, column: str, extra: str) -> None:
            if not consumers:
                # column changed but graph records no consumer — surface as warning
                gaps.append({"kind": kind, "column": column, "consumer": None,
                             "severity": "warning",
                             "detail": f"column '{column}' {extra}; no consumer "
                                       f"recorded in graph"})
                return
            for con in consumers:
                con_file = _file_of(conn, con)
                in_diff = con_file in changed_files if con_file else False
                severity = "warning" if in_diff else "fail"
                gaps.append({
                    "kind": kind, "column": column, "consumer": con,
                    "severity": severity,
                    "detail": (f"column '{column}' {extra}; consumer '{con}' "
                               f"({con_file or '?'}) "
                               + ("updated in this diff (confirm)"
                                  if in_diff else "NOT updated")),
                })

        for col in dropped:
            _emit("column_dropped", col, "dropped/renamed")
        for col in retyped:
            _emit("column_retyped", col,
                  f"retyped {base_cols[col]} -> {head_cols[col]}")
    finally:
        conn.close()

    return _verdict(
        gaps,
        clean_text="No DataFrame column gaps — consumers updated.",
        header="DataFrame column-schema changes (consumers may be stale):",
    )


# ── A-3 · SQL query projection contract ──────────────────────────────────────────

import re

_SELECT_RE = re.compile(r"\bselect\b(.*?)\bfrom\b", re.IGNORECASE | re.DOTALL)
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
# FROM/JOIN <table> — used to scope which readers a changed .sql file implicates.
_FROM_JOIN_RE = re.compile(r"\b(?:FROM|JOIN)\s+`?([A-Za-z_][\w.]*)`?", re.IGNORECASE)


def _sql_tables(text: str) -> set[str]:
    """Table names referenced by FROM/JOIN in a SQL string (PSG-C6)."""
    return {m.group(1) for m in _FROM_JOIN_RE.finditer(text or "")}


def _split_top_level(clause: str) -> list[str]:
    """Split a SELECT projection clause on top-level commas (ignore commas in
    parentheses, e.g. inside function calls)."""
    parts, depth, buf = [], 0, []
    for ch in clause:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def extract_sql_projection(sql: str) -> set[str]:
    """Return the set of projected output column NAMES from a SELECT statement.

    - `col`, `t.col`           -> 'col'
    - `expr AS alias`          -> 'alias'
    - `SUM(t.amount) AS total` -> 'total'
    - `*`                      -> '*'
    Best-effort, stdlib regex on a known query string (NOT diff text).
    """
    m = _SELECT_RE.search(sql)
    if not m:
        return set()
    clause = m.group(1).strip()
    if clause == "*":
        return {"*"}
    out: set[str] = set()
    for item in _split_top_level(clause):
        if item == "*":
            out.add("*")
            continue
        # alias wins: '... AS alias' or trailing 'expr alias'
        as_m = re.search(r"\bas\b\s+([A-Za-z_]\w*)\s*$", item, re.IGNORECASE)
        if as_m:
            out.add(as_m.group(1))
            continue
        # qualified or bare identifier: take the last identifier token
        idents = _IDENT_RE.findall(item)
        # drop SQL keywords that may appear (e.g. function names handled by alias)
        if idents:
            out.add(idents[-1])
    return out


def _sql_files(files: set) -> list[str]:
    return [f for f in files if f.endswith(".sql")]


def sql_contract(db_path: str, repo: str, base: str, head: str) -> dict:
    """Verdict for SQL query projection drift in changed .sql files.

    Treats a query's projected column-set as a contract fingerprint. When a
    column is REMOVED or RETYPED (best-effort: removal) and the downstream
    reader (resolved via reads_sql edge -> function file_path) is NOT in the
    diff's changed-files set -> FAIL. Added columns are surfaced as warnings.

    Returns {ok, gaps:[{kind, column, reader, severity, detail}], text}.
    """
    files = set(changed_files(repo, base, head))
    sql_files = _sql_files(files)
    if not sql_files:
        return {"ok": True, "gaps": [],
                "text": "No changed .sql files — no SQL projection contract change."}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    gaps: list[dict] = []
    try:
        for path in sql_files:
            base_text = git_show(repo, base, path)
            head_text = git_show(repo, head, path)
            base_cols = extract_sql_projection(base_text)
            head_cols = extract_sql_projection(head_text)
            removed = base_cols - head_cols
            added = head_cols - base_cols
            if not removed and not added:
                continue
            # PSG-C6: scope readers to those reading a table THIS file references,
            # instead of flagging every reads_sql reader in the repo. If we can't
            # parse any table name, fall back to all readers (don't miss a break).
            tables = _sql_tables(base_text) | _sql_tables(head_text)
            if tables:
                placeholders = ",".join("?" * len(tables))
                readers = conn.execute(
                    f"""SELECT DISTINCT src.name AS reader, src.file_path AS file
                        FROM edge e
                        JOIN edge_type et ON et.id = e.edge_type_id
                        JOIN node src ON src.id = e.src_node_id
                        JOIN node dst ON dst.id = e.dst_node_id
                        WHERE et.name = 'reads_sql' AND dst.name IN ({placeholders})""",
                    tuple(sorted(tables)),
                ).fetchall()
            else:
                readers = conn.execute(
                    """SELECT DISTINCT src.name AS reader, src.file_path AS file
                       FROM edge e
                       JOIN edge_type et ON et.id = e.edge_type_id
                       JOIN node src ON src.id = e.src_node_id
                       WHERE et.name = 'reads_sql'"""
                ).fetchall()
            for col in sorted(removed):
                if not readers:
                    gaps.append({"kind": "sql_column_removed", "column": col,
                                 "reader": None, "severity": "warning",
                                 "detail": f"SQL column '{col}' removed in {path}; "
                                           f"no reader recorded in graph"})
                for r in readers:
                    in_diff = r["file"] in files if r["file"] else False
                    severity = "warning" if in_diff else "fail"
                    gaps.append({
                        "kind": "sql_column_removed", "column": col,
                        "reader": r["reader"], "severity": severity,
                        "detail": (f"SQL column '{col}' removed in {path}; reader "
                                   f"'{r['reader']}' ({r['file'] or '?'}) "
                                   + ("updated in this diff (confirm)" if in_diff
                                      else "NOT updated")),
                    })
            for col in sorted(added):
                gaps.append({"kind": "sql_column_added", "column": col,
                             "reader": None, "severity": "warning",
                             "detail": f"SQL column '{col}' added in {path}; "
                                       f"downstream may want to consume it"})
    finally:
        conn.close()

    return _verdict(
        gaps,
        clean_text="No SQL projection gaps.",
        header="SQL query projection changes (downstream readers may be stale):",
    )
