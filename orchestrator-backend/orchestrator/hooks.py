"""Claude Code hook entry points (decision provenance phase 0/1).

`python -m orchestrator.hooks <event>` reads the hook's JSON on stdin and
records it: PostToolUse → one tool_call_log row (phase 0, what a plan costs);
UserPromptSubmit → the user's words verbatim into utterance (phase 1, Task 3);
PreToolUse (Edit | Write | MultiEdit) → the active constraints anchored on the
lines about to change (and one hop downstream) as additionalContext (phase 2,
Task 5) — additive, never a veto unless a HUMAN constraint is declared
block: true in the extensions file.

A hook process must never get in Claude Code's way: stdout stays EMPTY (a
UserPromptSubmit hook's stdout is injected as context), the exit code is
always 0 — PreToolUse is the one event that writes stdout, and only the hook
JSON — and anything that goes wrong is one line in the error log
(PROVLEDGER_HOOK_ERRORS, default ~/skill-workspace/hook-errors.log) that
selfcheck counts as hook_failures. The DB is opened with a 2 s busy timeout so
a locked orchestrator DB costs at most that.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db

DEFAULT_ERROR_LOG = Path.home() / "skill-workspace" / "hook-errors.log"
BUSY_TIMEOUT_MS = 2000
EVENTS = ("PostToolUse", "UserPromptSubmit", "PreToolUse")


def error_log_path() -> Path:
    return Path(os.environ.get("PROVLEDGER_HOOK_ERRORS") or DEFAULT_ERROR_LOG)


def db_path() -> Path:
    return Path(os.environ.get("ORCH_DB") or db.DEFAULT_DB_PATH)


def log_error(event: str, exc: BaseException) -> None:
    """One line per failure: time, event, exception class, message head. Never raises."""
    try:
        p = error_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            msg = str(exc).replace("\n", " ")[:200]
            f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {event or '-'} "
                    f"{type(exc).__name__}: {msg}\n")
    except Exception:  # pragma: no cover — the log itself failing must not surface
        pass


def _open():
    conn = db.open_db(db_path())
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    db.run_migrations(conn)
    return conn


def record_tool_call(conn, data: dict) -> int:
    cur = conn.execute("INSERT INTO tool_call_log (session_id, cwd, tool_name) VALUES (?, ?, ?)",
                       (str(data.get("session_id") or ""), data.get("cwd"), str(data.get("tool_name") or "")))
    conn.commit()
    return int(cur.lastrowid)


def _current_plan_id(conn, project: str | None) -> str | None:
    """The project's most recent IN_PROGRESS plan (None without a project or a plan)."""
    if not project:
        return None
    r = conn.execute("SELECT plan_id FROM Plans WHERE project = ? AND status = 'IN_PROGRESS' "
                     "ORDER BY created_at DESC, plan_id DESC LIMIT 1", (project,)).fetchone()
    return r[0] if r else None


def record_utterance(conn, data: dict) -> int | None:
    """UserPromptSubmit → one utterance row with the prompt VERBATIM. An empty
    prompt or a slash command is not a decision and is not recorded."""
    from . import provenance, psg_bridge
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or prompt.lstrip().startswith("/"):
        return None
    project = psg_bridge.project_for_cwd(data.get("cwd"))
    plan_id = _current_plan_id(conn, project)
    occurred_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return provenance.insert_utterance(conn, session_id=str(data.get("session_id") or ""), project=project,
                                       plan_id=plan_id, text=prompt, occurred_at=occurred_at)


def handle(event: str, data: dict) -> dict | None:
    """Dispatch one hook payload. Unknown events are ignored on purpose. Only
    PreToolUse may return something — the hook JSON main() prints."""
    if event == "PostToolUse":
        conn = _open()
        try:
            record_tool_call(conn, data)
        finally:
            conn.close()
    elif event == "UserPromptSubmit":
        conn = _open()
        try:
            record_utterance(conn, data)
        finally:
            conn.close()
    elif event == "PreToolUse":
        # the registry decides first (no DB, no PSG for a file outside every registered repo)
        tool = data.get("tool_name")
        ti = data.get("tool_input") or {}
        if tool not in EDIT_TOOLS or not isinstance(ti, dict) or not ti.get("file_path") \
                or _project_for_file(str(ti["file_path"]), data.get("cwd")) is None:
            return None
        conn = _open()
        try:
            return anchor_context(conn, data)
        finally:
            conn.close()
    return None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    event = argv[0] if argv else ""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("hook input is not a JSON object")
        out = handle(event, data)
        if out is not None and event == "PreToolUse":
            # the ONE case a hook writes stdout: the PreToolUse JSON (additionalContext)
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            sys.stdout.flush()
    except Exception as exc:
        log_error(event, exc)
    return 0




# ── PreToolUse (DP phase 2, Task 5): the constraints anchored on what is about to be edited ──

EDIT_TOOLS = ("Edit", "Write", "MultiEdit")
ANCHOR_CONTEXT_MAX_CHARS = 600
PSG_BUSY_TIMEOUT_MS = 500


def _project_for_file(file_path: str, cwd: str | None):
    """(project, repo) when file_path lies under a registered project's repo —
    decided from the registry alone, no database opened. None otherwise."""
    from . import psg_bridge
    path = os.path.abspath(file_path if os.path.isabs(file_path) else os.path.join(cwd or os.getcwd(), file_path))
    project = psg_bridge.project_for_cwd(os.path.dirname(path))
    if not project:
        return None
    repo = psg_bridge.repo_for(project)
    if not repo:
        return None
    return project, repo, path


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, max(offset, 0)) + 1


def edit_ranges(tool_name: str, tool_input: dict, path: str) -> list[tuple[int, int]]:
    """The line ranges an Edit / Write / MultiEdit is about to touch. Edit:
    where old_string sits in the file; Write: the whole existing file;
    MultiEdit: one range per edit. A string not found → no range."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    if tool_name == "Write":
        n = text.count("\n") + 1
        return [(1, n)] if text else []
    edits = tool_input.get("edits") if tool_name == "MultiEdit" else [tool_input]
    out = []
    for e in edits or []:
        old = (e or {}).get("old_string") or ""
        if not old:
            continue
        i = text.find(old)
        if i < 0:
            continue
        out.append((_line_of(text, i), _line_of(text, i + len(old) - 1)))
    return out


def anchor_context(conn, data: dict) -> dict | None:
    """The hook body. Returns the hook JSON (additionalContext, and a deny only
    for a HUMAN constraint declared block: true) or None when there is nothing
    to say — then not one byte goes to stdout. Every statement injected is a
    read_hit(moment='edit') with the injected size; the rationale never leaves."""
    from . import extensions, psg_bridge
    tool = data.get("tool_name")
    ti = data.get("tool_input") or {}
    if tool not in EDIT_TOOLS or not isinstance(ti, dict) or not ti.get("file_path"):
        return None
    hit = _project_for_file(str(ti["file_path"]), data.get("cwd"))
    if hit is None:
        return None                                   # not a registered repo: no PSG, no DB
    project, repo, path = hit
    ranges = edit_ranges(tool, ti, path)
    if not ranges:
        return None
    psg = psg_bridge.db_path_for(project)
    if not psg:
        return None
    rel = os.path.relpath(path, repo)
    nodes: dict[str, str] = {}
    for lo, hi in ranges:
        for n in psg_bridge.nodes_at(psg, rel, lo, hi):
            nodes.setdefault(n["node_key"], n["qualified_name"])
    if not nodes:
        return None
    downstream: dict[str, str] = {}
    for qn in list(nodes.values()):
        for nb in psg_bridge.card_of(psg, qn).get("output_consumers") or []:
            nk = psg_bridge.node_key_of(psg, nb)
            if nk and nk not in nodes:
                downstream.setdefault(nk, nb)
    anchors = {**nodes, **downstream}
    keys = list(anchors) + list(anchors.values())
    ph = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT id, node_key, statement, recorded_by, occurred_at FROM change_reason_v "
        f"WHERE project = ? AND role = 'constraint' AND state = 'active' AND superseded_by IS NULL AND node_key IN ({ph}) "
        f"ORDER BY id", (project, *keys)).fetchall()
    if not rows:
        return None
    hard = extensions.hard_statements(repo)
    lines, ids, denied, seen = [], [], [], set()
    for rid, nk, statement, by, at in rows:
        qn = anchors.get(nk) or nk
        ids.append(rid)                                   # every row behind a shown statement was shown
        if by == "human" and statement in hard:
            denied.append(f"{qn}: {statement}")
        if (qn, statement) in seen:                       # one constraint, several anchors / a rule's echo: say it once
            continue
        seen.add((qn, statement))
        where = "下游 " if nk in downstream else ""
        lines.append(f"- {where}{qn}: {statement} ({by}, {(at or '')[:10]})")
    first = next(iter(nodes.values()))
    text = "provledger · 这里有约束（来源等级见 why）：\n" + "\n".join(lines)
    if len(text) > ANCHOR_CONTEXT_MAX_CHARS:
        text = text[:ANCHOR_CONTEXT_MAX_CHARS - 1].rstrip() + "…"
    text += f"\n`provledger why {first}`"
    plan_id = _current_plan_id(conn, project)
    for rid in ids:
        conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, session_id, moment, injected_chars) VALUES (?, ?, ?, ?, 'edit', ?)",
                     (rid, project, plan_id, str(data.get("session_id") or "") or None, len(text)))
    conn.commit()
    out = {"hookEventName": "PreToolUse", "additionalContext": text}
    if denied:
        out["permissionDecision"] = "deny"
        out["permissionDecisionReason"] = "a human constraint declared block: true anchors here — " + "; ".join(denied)[:300] + " (answer it with provledger headline ack, or edit the extensions file)"
    return {"hookSpecificOutput": out}


if __name__ == "__main__":
    sys.exit(main())
