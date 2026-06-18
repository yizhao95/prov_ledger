"""Registry of project state graphs on record.

Source of truth = projects.json (machine-readable). The global index
PROJECT-STATE-GRAPHS.md is a human-readable view regenerated from it.

A project entry:
    {
      "name": str,            # unique key
      "repo": str,            # repo path or URL
      "db_path": str,         # location of the deep <name>-state-graph.db
      "commit_sha": str|None, # commit analyzed
      "updated_at": str,      # ISO-8601 UTC
    }
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(reg_path: str) -> Dict[str, Any]:
    if not os.path.exists(reg_path):
        return {"projects": []}
    with open(reg_path) as f:
        data = json.load(f)
    data.setdefault("projects", [])
    return data


def _save(reg_path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(reg_path)), exist_ok=True)
    with open(reg_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def list_projects(reg_path: str) -> List[Dict[str, Any]]:
    """Return all project entries (empty list if registry absent)."""
    return _load(reg_path)["projects"]


def get_project(reg_path: str, name: str) -> Optional[Dict[str, Any]]:
    for p in list_projects(reg_path):
        if p["name"] == name:
            return p
    return None


def add_project(
    reg_path: str,
    name: str,
    repo: str,
    db_path: str,
    commit_sha: Optional[str] = None,
) -> Dict[str, Any]:
    """Add or update (idempotent by name) a project entry."""
    data = _load(reg_path)
    entry = {
        "name": name,
        "repo": repo,
        "db_path": db_path,
        "commit_sha": commit_sha,
        "updated_at": _now(),
    }
    projects = [p for p in data["projects"] if p["name"] != name]
    projects.append(entry)
    projects.sort(key=lambda p: p["name"])
    data["projects"] = projects
    _save(reg_path, data)
    return entry


def regenerate_index(reg_path: str, index_path: str) -> str:
    """Write PROJECT-STATE-GRAPHS.md: high-level overview + location of each graph."""
    projects = list_projects(reg_path)
    lines = [
        "# Project State Graphs — Index",
        "",
        "High-level overview and locations of every project state graph on record.",
        "Generated from `projects.json` — do not edit by hand.",
        "",
        f"**Projects on record:** {len(projects)}",
        "",
    ]
    if projects:
        lines += [
            "| Project | Repo | Deep graph (sqlite) | Commit | Updated |",
            "|---|---|---|---|---|",
        ]
        for p in projects:
            sha = (p.get("commit_sha") or "")[:10]
            lines.append(
                f"| {p['name']} | {p['repo']} | `{p['db_path']}` | {sha} | {p['updated_at']} |"
     )
    else:
        lines.append("_No projects on record yet._")
    lines.append("")
    lines += [
        "## How to read a project graph",
        "",
        "- **Shallow layer:** `ARCHITECTURE.md` beside each deep graph — high-level",
        "  overview, file locations, and subsystem grouping.",
        "- **Deep layer:** the `*-state-graph.db` SQLite database — nodes, edges,",
        "  `data_var`, `consistency_card`, and `symbol_card` for precise impact analysis.",
        "",
    ]
    text = "\n".join(lines)
    os.makedirs(os.path.dirname(os.path.abspath(index_path)), exist_ok=True)
    with open(index_path, "w") as f:
        f.write(text)
    return text
