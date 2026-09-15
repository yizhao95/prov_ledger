"""Claude Code hook entry points (decision provenance phase 0/1).

`python -m orchestrator.hooks <event>` reads the hook's JSON on stdin and
records it: PostToolUse → one tool_call_log row (phase 0, what a plan costs);
UserPromptSubmit → the user's words verbatim into utterance (phase 1, Task 3).

A hook process must never get in Claude Code's way: stdout stays EMPTY (a
UserPromptSubmit hook's stdout is injected as context), the exit code is
always 0, and anything that goes wrong is one line in the error log
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
EVENTS = ("PostToolUse", "UserPromptSubmit")


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


def handle(event: str, data: dict) -> None:
    """Dispatch one hook payload. Unknown events are ignored on purpose."""
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


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    event = argv[0] if argv else ""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("hook input is not a JSON object")
        handle(event, data)
    except Exception as exc:
        log_error(event, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
