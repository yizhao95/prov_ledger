"""Shallow-layer ARCHITECTURE.md generator.

Reads a *built* project state-graph DB (see analyzer/store.py for schema) and
emits a human-readable Markdown overview: a repo summary, the full file list
grouped by subsystem (top-level dir/module), and pointers to key symbols that
can be SELECTed from the deep DB for precise impact analysis.

This is the "shallow layer" — fast to read, points you at the "deep layer"
(the sqlite graph) when you need detail.
"""
from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# Node types we treat as "key symbols" worth surfacing in the shallow view.
_SYMBOL_TYPES = ("function", "method", "class")


def _subsystem_of(file_path: str) -> str:
    """Top-level dir/module is the subsystem. Files at the root -> '(root)'."""
    norm = file_path.replace("\\", "/").lstrip("./")
    head, _, tail = norm.partition("/")
    return head if tail else "(root)"


def _fetch_files(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT DISTINCT file_path FROM node "
        "WHERE file_path IS NOT NULL AND file_path != '' "
        "ORDER BY file_path"
    ).fetchall()
    return [r[0] for r in rows]


def _fetch_symbols(conn: sqlite3.Connection) -> List[Tuple[str, str, str]]:
    """Return (file_path, type_name, symbol_name) for key symbol nodes."""
    placeholders = ",".join("?" for _ in _SYMBOL_TYPES)
    rows = conn.execute(
        f"""SELECT n.file_path, t.name, n.name
            FROM node n JOIN node_type t ON n.node_type_id = t.id
            WHERE t.name IN ({placeholders})
            ORDER BY n.file_path, t.name, n.name""",
        _SYMBOL_TYPES,
    ).fetchall()
    return [(r[0] or "(unknown)", r[1], r[2]) for r in rows]


def _fetch_run(conn: sqlite3.Connection) -> Optional[Tuple[str, Optional[str]]]:
    row = conn.execute(
        "SELECT project_name, commit_sha FROM analysis_run "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return (row[0], row[1]) if row else None


def generate(db_path: str, repo_name: str) -> str:
    """Build the ARCHITECTURE.md markdown from a built state-graph DB."""
    conn = sqlite3.connect(db_path)
    try:
        files = _fetch_files(conn)
        symbols = _fetch_symbols(conn)
        run = _fetch_run(conn)
        node_count = conn.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    finally:
        conn.close()

    commit_sha = run[1] if run else None

    # Group files + symbols by subsystem.
    files_by_sub: Dict[str, List[str]] = defaultdict(list)
    for f in files:
        files_by_sub[_subsystem_of(f)].append(f)

    symbols_by_file: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for file_path, type_name, sym in symbols:
        symbols_by_file[file_path].append((type_name, sym))

    lines: List[str] = []
    lines.append(f"# {repo_name} — Architecture")
    lines.append("")
    lines.append(
        "Shallow-layer overview generated from the project state graph. "
        "For precise detail, query the deep layer (`*-state-graph.db`)."
    )
    lines.append("")

    # ── Overview ──────────────────────────────────────────────
    lines.append("## Overview")
    lines.append("")
    lines.append(f"- **Repo:** {repo_name}")
    lines.append(f"- **Files:** {len(files)}")
    lines.append(f"- **Subsystems:** {len(files_by_sub)}")
    lines.append(f"- **Graph nodes:** {node_count}")
    lines.append(f"- **Graph edges:** {edge_count}")
    if commit_sha:
        lines.append(f"- **Commit:** `{commit_sha}`")
    lines.append("")

    # ── Subsystems ────────────────────────────────────────────
    lines.append("## Subsystems")
    lines.append("")
    for sub in sorted(files_by_sub):
        sub_files = files_by_sub[sub]
        lines.append(f"### {sub}")
        lines.append("")
        for f in sorted(sub_files):
            syms = symbols_by_file.get(f, [])
            if syms:
                rendered = ", ".join(
                    f"`{name}` ({t})" for t, name in syms
                )
                lines.append(f"- `{f}` — {rendered}")
            else:
                lines.append(f"- `{f}`")
        lines.append("")

    # ── Key symbols / deep-layer pointers ─────────────────────
    lines.append("## Key symbols (deep layer)")
    lines.append("")
    lines.append(
        "Query these from the deep DB for impact analysis, e.g.:"
    )
    lines.append("")
    lines.append("```sql")
    lines.append("-- find a symbol and its location")
    lines.append("SELECT n.name, n.file_path, n.line_start, t.name AS kind")
    lines.append("FROM node n JOIN node_type t ON n.node_type_id = t.id")
    lines.append("WHERE t.name IN ('function','method','class');")
    lines.append("")
    lines.append("-- find callers/callees of a symbol")
    lines.append("SELECT * FROM edge WHERE src_node_id = ? OR dst_node_id = ?;")
    lines.append("```")
    lines.append("")
    if symbols:
        lines.append("| Symbol | Kind | File |")
        lines.append("|---|---|---|")
        for file_path, type_name, sym in symbols:
            lines.append(f"| `{sym}` | {type_name} | `{file_path}` |")
        lines.append("")

    return "\n".join(lines)


def write_file(db_path: str, repo_name: str, out_path: str) -> str:
    """Generate and write ARCHITECTURE.md to out_path. Returns the markdown."""
    md = generate(db_path, repo_name)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(md)
        if not md.endswith("\n"):
            f.write("\n")
    return md


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("usage: architecture_md.py <db_path> <repo_name> [out_path]")
        sys.exit(2)
    _db, _name = sys.argv[1], sys.argv[2]
    if len(sys.argv) >= 4:
        write_file(_db, _name, sys.argv[3])
        print(f"wrote {sys.argv[3]}")
    else:
        print(generate(_db, _name))
