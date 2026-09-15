"""Calibration export (phase 7 Task 2): every identity_ambiguous event of a
state-graph DB as a calibration item — layer, the previous and current rows
with their three identity layers (qualified_name / struct_sig / dataflow_sig),
file + lines, and ±10 lines of source context read from the run's commit
(`git show <sha>:<path>`) when a repo is given, else from the working tree.
`truth` and `labelled_by` are left null for a person to fill; nothing here
decides anything.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import store
from ._host import testing as _testing

CONTEXT_LINES = 10


def _snapshot(conn, run_id: int | None, qualified_name: str) -> dict | None:
    if run_id is None:
        return None
    r = conn.execute(
        """SELECT node_key, node_type, qualified_name, file_path, line_start, line_end, struct_sig, dataflow_sig, dataflow_trivial
           FROM node_snapshot WHERE run_id=? AND qualified_name=? ORDER BY id LIMIT 1""", (run_id, qualified_name)).fetchone()
    if not r:
        return None
    return {"node_key": r[0], "node_type": r[1], "qualified_name": r[2], "file_path": r[3], "line_start": r[4],
            "line_end": r[5], "struct_sig": r[6], "dataflow_sig": r[7], "dataflow_trivial": bool(r[8])}


def _source(repo: str | None, sha: str | None, path: str | None) -> list[str] | None:
    if not path:
        return None
    if repo and sha:
        try:
            out = subprocess.run(["git", "-C", repo, "show", f"{sha}:{path}"], capture_output=True, text=True, check=True).stdout
            return out.splitlines()
        except (subprocess.CalledProcessError, OSError):
            pass
    p = Path(repo or ".") / path
    if p.exists():
        try:
            return p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
    return None


def _context(lines: list[str] | None, line_start, line_end) -> str | None:
    if not lines or not line_start:
        return None
    lo = max(1, int(line_start) - CONTEXT_LINES)
    hi = min(len(lines), int(line_end or line_start) + CONTEXT_LINES)
    return "\n".join(f"{n:5d}  {lines[n - 1]}" for n in range(lo, hi + 1))


def export(conn, out_path, repo: str | None = None) -> dict:
    """Write the calibration file to out_path and return it."""
    runs = {r[0]: r[1] for r in conn.execute("SELECT id, commit_sha FROM analysis_run")}
    events = conn.execute(
        """SELECT e.id, e.run_id, e.payload_json FROM node_event e JOIN analysis_run a ON a.id=e.run_id
           WHERE e.event_type='identity_ambiguous' AND COALESCE(a.aborted, 0)=0 ORDER BY e.run_id, e.seq""").fetchall()
    items = []
    cache: dict[tuple, list[str] | None] = {}
    db_name = Path(conn.execute("PRAGMA database_list").fetchone()[2] or "").name or "graph.db"
    for event_id, run_id, payload_json in events:
        payload = json.loads(payload_json)
        prev_run = store.previous_run_id(conn, run_id)
        sides = {}
        for side, rid, sha in (("prev", prev_run, runs.get(prev_run)), ("cur", run_id, runs.get(run_id))):
            rows = []
            keys = payload.get("prev_keys") or []
            for i, qn in enumerate(payload.get(side) or []):
                snap = _snapshot(conn, rid, qn) or {"qualified_name": qn, "node_key": (keys[i] if side == "prev" and i < len(keys) else ""),
                                                    "node_type": None, "file_path": None, "line_start": None,
                                                    "line_end": None, "struct_sig": None, "dataflow_sig": None}
                if side == "prev" and i < len(keys):
                    snap["node_key"] = keys[i]
                k = (sha, snap.get("file_path"))
                if k not in cache:
                    cache[k] = _source(repo, sha, snap.get("file_path"))
                snap["context"] = _context(cache[k], snap.get("line_start"), snap.get("line_end"))
                rows.append(snap)
            sides[side] = rows
        items.append({"id": f"e{event_id}", "source": f"live:{db_name}",
                      "ambiguity": {"layer": payload.get("layer"), "run_id": run_id, "prev_run_id": prev_run,
                                    "commit_sha": runs.get(run_id), "prev_commit_sha": runs.get(prev_run),
                                    "same_struct_sig": payload.get("same_struct_sig"),
                                    "same_dataflow_sig": payload.get("same_dataflow_sig"),
                                    "prev": sides["prev"], "cur": sides["cur"]},
                      "truth": None, "labelled_by": None})
    doc = {"version": _testing.calibration.VERSION, "source": {"db_path": str(conn.execute("PRAGMA database_list").fetchone()[2]),
                                                           "repo": repo, "events": len(items)}, "items": items}
    Path(out_path).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return doc
