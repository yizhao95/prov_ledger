"""Claude Code hook entry points (decision provenance phase 0/1).

`python -m orchestrator.hooks <event>` reads the hook's JSON on stdin and
records it: PostToolUse / PostToolUseFailure → one tool_call_log row, failed ones marked (phase 0, what a plan costs);
UserPromptSubmit → the user's words verbatim into utterance (phase 1, Task 3),
plus the source-mention hint when the sentence named an outside source (A3);
PreToolUse (Edit | Write | MultiEdit) → the active constraints anchored on the
lines about to change (and one hop downstream) as additionalContext (phase 2,
Task 5) — additive, never a veto unless a HUMAN constraint is declared
block: true in the extensions file; Stop → session_run and, in the degraded
mode (no plan in the session), a queued background graph refresh (Task 7b).

A hook process must never get in Claude Code's way. The exit code is always 0,
and only two events write stdout at all: PreToolUse prints its hook JSON, and
UserPromptSubmit prints at most one plain line — its stdout is injected as
context, so that line is an addition to the turn and never a condition on it.
Every other event stays silent. Anything that goes wrong is one line in the
error log (PROVLEDGER_HOOK_ERRORS, default ~/skill-workspace/hook-errors.log)
that selfcheck counts as hook_failures. The DB is opened with a 2 s busy timeout
so a locked orchestrator DB costs at most that.
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
# What counts as the user speaking is decided in one place, provenance.py: the
# rules that turn words into a `stated` reason read it too (FL-238).
from .provenance import INJECTED_PROMPT_PREFIXES, is_injected_prompt  # noqa: F401

DEFAULT_ERROR_LOG = Path.home() / "skill-workspace" / "hook-errors.log"
BUSY_TIMEOUT_MS = 2000
EVENTS = ("PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "PreToolUse", "Stop")
# The events whose row is spooled when the ledger is locked (FL-193). Stop is not:
# replayed later it would queue a graph refresh at an arbitrary moment and stamp
# the session's end with the wrong time.
SPOOLED_EVENTS = ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure")
HEADLESS_ENV = "PROVLEDGER_HEADLESS"   # set by testing.claude_arbiter on its claude child


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


def command_head(data: dict) -> str | None:
    """The first 80 chars of a Bash tool's command (DP phase 2, Task 6: what
    part of a plan is provledger itself); NULL for every other tool."""
    if str(data.get("tool_name") or "") != "Bash":
        return None
    ti = data.get("tool_input")
    cmd = ti.get("command") if isinstance(ti, dict) else None
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    return cmd.strip().replace("\n", " ")[:80]


def record_tool_call(conn, data: dict, *, failed: bool = False, at: str | None = None,
                     commit: bool = True) -> int:
    """One tool_call_log row. A failed call arrives as PostToolUseFailure (FL-208).
    `at` is given only when a spooled call is replayed: the time it happened, not
    the time it finally reached the ledger."""
    cols = "session_id, cwd, tool_name, command_head, failed" + (", at" if at else "")
    vals = [str(data.get("session_id") or ""), data.get("cwd"), str(data.get("tool_name") or ""),
            command_head(data), 1 if failed else 0] + ([at] if at else [])
    cur = conn.execute(f"INSERT INTO tool_call_log ({cols}) VALUES ({', '.join('?' * len(vals))})", vals)
    if commit:
        conn.commit()
    return int(cur.lastrowid)



def _current_plan_id(conn, project: str | None) -> str | None:
    """The project's most recent IN_PROGRESS plan (None without a project or a plan)."""
    if not project:
        return None
    r = conn.execute("SELECT plan_id FROM Plans WHERE project = ? AND status = 'IN_PROGRESS' "
                     "ORDER BY created_at DESC, plan_id DESC LIMIT 1", (project,)).fetchone()
    return r[0] if r else None


def record_utterance(conn, data: dict, *, occurred_at: str | None = None, commit: bool = True) -> int | None:
    """UserPromptSubmit → one utterance row with the prompt VERBATIM. An empty
    prompt, a slash command, or text Claude Code itself injected (DP phase 2d,
    Task 0) is not a decision and is not recorded."""
    from . import provenance, psg_bridge
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or prompt.lstrip().startswith("/"):
        return None
    if is_injected_prompt(prompt):
        return None
    project = psg_bridge.project_for_cwd(data.get("cwd"))
    plan_id = _current_plan_id(conn, project)
    occurred_at = occurred_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # origin='hook' (A1) is this path's one privilege and it is not transferable:
    # the words arrived through UserPromptSubmit, so this is the only writer that
    # can say they were captured as they were typed rather than recalled later.
    return provenance.insert_utterance(conn, session_id=str(data.get("session_id") or ""), project=project,
                                       plan_id=plan_id, text=prompt, occurred_at=occurred_at, origin="hook",
                                       commit=commit)


# A3: the sentence named a source, so ask for the pointer while it is still cheap.
# One line, and it only ever asks. `--utterance` rather than `--reason` because at
# UserPromptSubmit no plan exists yet, so no reason exists either; migration 030 lets
# the pointer hang on the words, and reason-fill carries it across later.
SOURCE_HINT = (
    'provledger: that sentence points at an outside source ("{word}"). If you have a tool that can locate it, '
    'record the pointer: provledger reference add --utterance {uid} '
    '--kind email|meeting|chat|ticket|doc|commit --label "<subject · who>" --occurred-at "<when>" --uri <permalink>'
)


def source_hint(utterance_id: int | None, prompt: str | None) -> str | None:
    """One line of context when the words just recorded named an outside source.

    None whenever there is nothing honest to say: no utterance was written (a slash
    command, an empty prompt, a failed database), so there is no id for a pointer to
    hang on; or the text is Claude Code's own injected output, which is filtered here
    as well as upstream — a task-notification full of the word "email" must never
    make the hook ask the user to pin a source for a sentence they never said.

    The rule is deterministic string matching (see testing.source_words), so it is a
    rule and not a model feature: no gate, no runner, nothing fetched.
    """
    if not utterance_id or not isinstance(prompt, str):
        return None
    if is_injected_prompt(prompt):
        return None
    from .testing import source_words
    word = source_words.mentions_a_source(prompt)
    if not word:
        return None
    return SOURCE_HINT.format(word=word, uid=int(utterance_id))


# ── the spool (FL-193): a row that cannot be written now is written by the next hook ──

def spool_path(ledger: Path | str | None = None) -> Path:
    """Beside the ledger: `orchestrator.db` -> `orchestrator.db.spool.jsonl`."""
    p = Path(ledger) if ledger is not None else db_path()
    return p.with_name(p.name + ".spool.jsonl")


def _busy(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(w in str(exc).lower() for w in ("locked", "busy"))


def _stamp(event: str) -> str:
    """The time an event happened, in the format of the column it lands in. The
    system clock, not the DB's: the DB is exactly what could not be reached."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S") if event == "UserPromptSubmit" else now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(text)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def spool(event: str, data: dict, occurred_at: str) -> None:
    _append(spool_path(), json.dumps({"event": event, "occurred_at": occurred_at, "data": data},
                                     ensure_ascii=False) + "\n")


def replay_spool(conn) -> int:
    """Write the spooled rows, oldest first, with the times they happened, in one
    transaction. The write lock is taken first and the spool is then moved aside
    under its file lock, so a second hook waiting for the lock finds it gone and
    nothing is written twice. On failure the lines go back for the next hook. A
    line that cannot be read is skipped and logged. The plan an utterance is
    attributed to is the one open when it is replayed."""
    p = spool_path()
    if not p.exists():
        return 0
    conn.execute("BEGIN IMMEDIATE")
    mine = p.with_name(f"{p.name}.{os.getpid()}")
    try:
        with p.open("a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                os.replace(p, mine)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        conn.rollback()
        return 0
    text = mine.read_text(encoding="utf-8")
    done = 0
    try:
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                event, data, at = e["event"], e["data"], e["occurred_at"]
            except (ValueError, KeyError, TypeError):
                log_error("spool", ValueError(f"an unreadable spool line was skipped: {line[:80]}"))
                continue
            if event == "UserPromptSubmit":
                record_utterance(conn, data, occurred_at=at, commit=False)
            elif event in ("PostToolUse", "PostToolUseFailure"):
                record_tool_call(conn, data, failed=(event == "PostToolUseFailure"), at=at, commit=False)
            done += 1
        conn.commit()
    except Exception:
        conn.rollback()
        _append(p, text)
        mine.unlink(missing_ok=True)
        raise
    mine.unlink(missing_ok=True)
    return done


def _write(event: str, data: dict, fn):
    """Open, replay the spool, write this event's row with `fn(conn)`. When the
    ledger is locked past the busy timeout, a spooled event's row goes to the
    spool instead of being lost; returns None then."""
    at = _stamp(event)
    try:
        conn = _open()
    except Exception as exc:
        if event in SPOOLED_EVENTS and _busy(exc):
            spool(event, data, at)
            log_error(event, exc)
            return None
        raise
    try:
        try:
            replay_spool(conn)
        except Exception as exc:          # the spool never stops this event's own row
            if conn.in_transaction:
                conn.rollback()
            log_error("spool", exc)
        return fn(conn)
    except Exception as exc:
        if event in SPOOLED_EVENTS and _busy(exc):
            spool(event, data, at)
            log_error(event, exc)
            return None
        raise
    finally:
        conn.close()


def handle(event: str, data: dict) -> dict | str | None:
    """Dispatch one hook payload. Unknown events are ignored on purpose. Two events
    may return something for main() to print: PreToolUse its hook JSON, and
    UserPromptSubmit the one-line source hint (A3).

    Nothing at all inside provLedger's own headless `claude -p` calls (FL-182):
    the runner marks its child with PROVLEDGER_HEADLESS=1, and a prompt
    provLedger wrote to a model is not something the user said."""
    if os.environ.get(HEADLESS_ENV) == "1":
        return None
    if event in ("PostToolUse", "PostToolUseFailure"):
        _write(event, data, lambda conn: record_tool_call(conn, data, failed=(event == "PostToolUseFailure")))
    elif event == "UserPromptSubmit":
        uid = _write(event, data, lambda conn: record_utterance(conn, data))
        # The words are down first (or spooled), and they stay down whatever the
        # rule decides: the hint is an addition to the turn, never a condition on it.
        return source_hint(uid, data.get("prompt"))
    elif event == "Stop":
        # DP phase 2 (Task 7b): the degraded mode — record the session, queue a graph
        # refresh when nothing else will; stdout stays empty
        from . import session
        _write(event, data, lambda conn: session.on_stop(conn, data, orch_db_path=str(db_path())))
        return None
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
        if out is None:
            pass
        elif event == "PreToolUse":
            # the hook JSON (additionalContext, and a deny only for a declared hard constraint)
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            sys.stdout.flush()
        elif event == "UserPromptSubmit":
            # A UserPromptSubmit hook's stdout is injected as context, so this is one
            # plain line and nothing else — no hook JSON, no permission field, nothing
            # that could turn an offer into a condition.
            sys.stdout.write(out.rstrip("\n") + "\n")
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
        where = "downstream " if nk in downstream else ""
        lines.append(f"- {where}{qn}: {statement} ({by}, {(at or '')[:10]})")
    first = next(iter(nodes.values()))
    text = "provledger · there are constraints here (source levels are in `why`):\n" + "\n".join(lines)
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
