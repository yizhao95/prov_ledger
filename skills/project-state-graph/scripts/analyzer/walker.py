"""File discovery: walks a repo, records one `file` node per supported file."""
from __future__ import annotations

import os
import sqlite3
from typing import Dict

from . import store

IGNORE_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git",
               ".pytest_cache", ".mypy_cache", "dist", "build",
               # generated coverage / report artifacts (can be thousands of files)
               "htmlcov", ".tox", ".nox", ".eggs", "site-packages"}

EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".sql": "sql",
}


def language_for(path: str) -> str | None:
    _, ext = os.path.splitext(path)
    return EXT_LANG.get(ext.lower())


def walk(conn: sqlite3.Connection, repo_root: str) -> Dict[str, int]:
    """Discover supported files under repo_root, recording a `file` node each.

    Returns a mapping of relative file_path -> node id.
    """
    repo_root = os.path.abspath(repo_root)
    file_type = store.get_or_create_node_type(conn, "file")
    result: Dict[str, int] = {}

    for dirpath, dirnames, filenames in os.walk(repo_root):
        # prune ignored directories in-place
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fname in filenames:
            lang = language_for(fname)
            if lang is None:
                continue
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, repo_root)
            node_id = store.add_node(
                conn,
                file_type,
                name=fname,
                qualified_name=rel_path,
                file_path=rel_path,
                metadata={"language": lang},
            )
            result[rel_path] = node_id
    return result
