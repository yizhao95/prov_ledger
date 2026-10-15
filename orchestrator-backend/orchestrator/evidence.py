"""evidence — A6: which tasks are worth looking for a source for, what backs a
record, and what became of each look.

Three parts, and provLedger does no searching in any of them.

`slots_for_plan` is the to-check list: one entry per task reason of the plan
whose root has no checkable source yet (task-level redesign, step 3b — a task's
evidence is where its cause was set, a meeting or a mail, looked for once; not a
source per changed function). Each slot carries the task, the nodes it covers,
its root and the root's words, the plan's own time window, the identifiers to
search on, and where a source that turns up should hang. The host agent takes
that list to its own mailbox, chat, notes and tracker with its own credentials,
and whatever it finds comes back through `provledger reference add` — with who
said it and the passage they said. Nothing here opens a link, and nothing here
asks a model anything: the hints are identifiers pulled out of the graph and out
of the words already recorded, by rule.

`sources_for_reason` is the one read of what backs a record: the sources hung on
the reason itself, on the words it quotes and — for a task reason — on its
root's words and on the root plan's task reasons. Every surface that prints a
source reads it here.

`record` is the audit trail. Every slot's ending is written to `evidence_log`,
so "why is this one blank" has an answer: nobody searched, or somebody searched
and found nothing, or the search ran out of time. A system that degrades in
silence is the thing this product exists to prevent, so the silence is the one
thing that is not allowed to go unrecorded.

One cost gate from A6 stays: the window is the plan's own span rather than the
project's history, and at most `PROVLEDGER_EVIDENCE_SLOTS` (default 5) slots
come back, in the order the tasks were recorded.
"""
from __future__ import annotations

import os
import re

from . import psg_bridge

DEFAULT_SLOTS = 5
SLOTS_ENV = "PROVLEDGER_EVIDENCE_SLOTS"

# What the host may be told about a slot's ending. Three of them are the reason a
# row can be blank, and they are kept apart on purpose: `not_searched` never
# entered the search (no tool, or the cap cut it off), `found_nothing` searched
# the window and the window held nothing, `timed_out` started and ran out of time.
# Collapsing them into one "no evidence" is how a degradation becomes invisible.
OUTCOMES = ("attached", "found_nothing", "timed_out", "not_searched")
TASK_CHARS = 400
MAX_HINTS = 12
_MIN_PART = 3
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_RE = re.compile(r"[a-z][A-Z]")


# ── the cap ──────────────────────────────────────────────────────────────────

def slot_cap() -> int:
    """`PROVLEDGER_EVIDENCE_SLOTS`, or 5. Anything that is not a whole number
    reads as the default: a mistyped budget must not silently mean "no limit"."""
    raw = os.environ.get(SLOTS_ENV)
    if raw is None or not str(raw).strip().lstrip("+").isdigit():
        return DEFAULT_SLOTS
    return int(str(raw).strip())


# ── the hints: identifiers, by rule ──────────────────────────────────────────

def _name_parts(qualified_name: str | None) -> list[str]:
    """The node's own name first, then the containers around it.

    The root segment is dropped when there is anything else: a package name says
    which repo this is, which the host agent already knows, and searching a
    mailbox for it returns the whole mailbox.
    """
    segs = [s for s in re.split(r"[.:]+", qualified_name or "") if s]
    kept = [s for s in segs if len(s) >= _MIN_PART]
    if len(kept) > 1 and kept[0] == segs[0]:
        kept = kept[1:]
    return list(reversed(kept))


def _identifier_like(token: str) -> bool:
    """Whether a word from recorded prose is worth searching for.

    An identifier, an acronym or something with a number in it — `weekly_report`,
    `EMEA`, `Q3`. A plain lower-case English word is not: "excluded" would match
    half the mailbox, and the point of a hint is to narrow.
    """
    if len(token) < 2:
        return False
    if "_" in token or any(c.isdigit() for c in token):
        return True
    return token.isupper() or bool(_CAMEL_RE.search(token))


class _Hints:
    """An ordered, case-insensitively de-duplicated list, cut at MAX_HINTS."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self._seen: set[str] = set()

    def add(self, word: str) -> None:
        if word.lower() not in self._seen:
            self._seen.add(word.lower())
            self.out.append(word)

    def words_of(self, text: str | None) -> None:
        for token in _IDENT_RE.findall(text or ""):
            if _identifier_like(token):
                self.add(token)


def hints_for(qualified_name: str | None, what_changed: str = "") -> list[str]:
    """The identifiers to search on for one node: its names, then the
    identifier-shaped words of what was recorded about it."""
    h = _Hints()
    for part in _name_parts(qualified_name):
        h.add(part)
    h.words_of(what_changed)
    return h.out[:MAX_HINTS]


def task_hints(node_names: list[str], *texts: str | None) -> list[str]:
    """The identifiers to search on for one task: the identifier-shaped words of
    the texts first (the root's words, the task's reason — what is looked for is
    where the cause was set), then each node's own name, then the containers
    around the nodes. Deterministic, order stable."""
    h = _Hints()
    for text in texts:
        h.words_of(text)
    parts = [_name_parts(n) for n in node_names]
    for p in parts:
        if p:
            h.add(p[0])
    for p in parts:
        for part in p[1:]:
            h.add(part)
    return h.out[:MAX_HINTS]


# ── the window: this plan, not this project ──────────────────────────────────

def _now(conn) -> str:
    return conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now')").fetchone()[0]


def plan_window(conn, plan_id: str) -> list[str] | None:
    """[start, end] of the plan's own span, or None when nothing dates it.

    A plan still open ends its window now rather than at the end of time: the
    window bounds the host agent's search, and an unbounded end bounds nothing.
    `session:<sid>` takes its span from session_run.
    """
    row = conn.execute("SELECT created_at, completed_at FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    if row is None and str(plan_id).startswith("session:"):
        row = conn.execute("SELECT started_at, ended_at FROM session_run WHERE session_id = ?",
                           (str(plan_id).split(":", 1)[1],)).fetchone()
    if row is None or not row[0]:
        return None
    return [row[0], row[1] or _now(conn)]


# ── what backs a record ──────────────────────────────────────────────────────

_SOURCE_COLS = ("f.id AS reference_id, f.kind, f.label, f.uri, f.occurred_at, f.verifiability, f.visibility, "
                "f.last_checked, f.said_by, f.excerpt, l.stance")


def _root_anchor(conn, plan_id: str) -> dict | None:
    """{root_plan_id, utterance_id, words} of the plan's root, or None."""
    from . import roots
    root = roots.root_of(conn, plan_id)
    if not root:
        return None
    r = conn.execute("SELECT utterance_id FROM plan_root WHERE plan_id = ? AND kind = 'new' ORDER BY id DESC LIMIT 1",
                     (root,)).fetchone()
    return {"root_plan_id": root, "words": roots._words(conn, root),
            "utterance_id": r["utterance_id"] if r else None}


def _anchors(conn, reason: dict) -> list[tuple[str, str, int]]:
    """(via, column, id) in the order a reader should meet them: the reason, the
    words it quotes, and for a task reason its root's words and the root plan's
    task reasons."""
    out = [("reason", "reason_id", reason["id"])]
    if reason.get("verbatim_utterance_id"):
        out.append(("quoted words", "utterance_id", reason["verbatim_utterance_id"]))
    if reason.get("role") == "reason" and reason.get("node_key") is None:
        root = _root_anchor(conn, reason["plan_id"])
        if root and root["utterance_id"]:
            out.append(("root's words", "utterance_id", root["utterance_id"]))
        if root and root["root_plan_id"] != reason["plan_id"]:
            from . import reasons
            for r in reasons._live_task_reasons(conn, root["root_plan_id"]):
                out.append(("root plan's task", "reason_id", r["id"]))
    return out


def sources_for_reason(conn, reason_id: int) -> list[dict]:
    """Every source that backs this record, each once, by the nearest anchor.

    A passage marked `personal` does not travel: its words stay in the ledger and
    the read says they were withheld, the same rule as a personal utterance."""
    row = conn.execute("SELECT id, plan_id, node_key, role, verbatim_utterance_id FROM change_reason WHERE id = ?",
                       (reason_id,)).fetchone()
    if row is None:
        return []
    out, seen = [], set()
    for via, col, anchor in _anchors(conn, dict(row)):
        for r in conn.execute(f"SELECT {_SOURCE_COLS} FROM reference_link l JOIN reference f ON f.id = l.reference_id "
                              f"WHERE l.{col} = ? ORDER BY f.id", (anchor,)):
            if r["reference_id"] in seen:
                continue
            seen.add(r["reference_id"])
            src = {**dict(r), "via": via, "excerpt_withheld": False}
            if src["visibility"] == "personal" and src["excerpt"]:
                src["excerpt"], src["excerpt_withheld"] = None, True
            out.append(src)
    return out


# ── the slots ────────────────────────────────────────────────────────────────

def _task_text(conn, reason: dict) -> str | None:
    """What the task reason says, in the words it was recorded in — the span of
    the user's own sentence when there is one, otherwise the recorded reading.
    Never a paraphrase."""
    if reason.get("verbatim_utterance_id"):
        row = conn.execute("SELECT substr(text, ? + 1, ? - ?) FROM utterance WHERE id = ?",
                           (reason["verbatim_start"], reason["verbatim_end"], reason["verbatim_start"],
                            reason["verbatim_utterance_id"])).fetchone()
        if row and row[0]:
            return str(row[0]).strip()[:TASK_CHARS]
    for col in ("interpretation", "statement"):
        if reason.get(col):
            return str(reason[col]).strip()[:TASK_CHARS]
    return None


def _covered(conn, plan_id: str, psg_db_path: str | None, reason_ids: list[int]) -> dict[int, list[str]]:
    """{reason_id: sorted names of the changed nodes it covers}."""
    from . import reasons
    out: dict[int, list[str]] = {rid: [] for rid in reason_ids}
    for node in psg_bridge.changed_node_keys(psg_db_path, plan_id) if psg_db_path else []:
        for rid in reasons.covering_task_reasons(conn, node["node_key"], [plan_id]):
            if rid in out:
                out[rid].append(node.get("qualified_name") or node["node_key"])
    return {rid: sorted(names) for rid, names in out.items()}


def _attach_with(attach_to: dict) -> str:
    anchor = (f"--utterance {attach_to['utterance_id']}" if "utterance_id" in attach_to
              else f"--reason {attach_to['reason_id']}")
    return (f"provledger reference add {anchor} --kind meeting|email|chat|ticket|doc --uri <permalink> "
            f'--label "<what it is · when>" --occurred-at <when> --said-by "<who said it>" '
            f'--excerpt "<the passage, as they said it>" [--stance contradicts]')


def slots_for_plan(conn, *, project: str, plan_id: str, psg_db_path: str | None = None,
                   limit: int | None = None) -> list[dict]:
    """The plan's task reasons whose root still has no checkable source.

    A task drops out as soon as a `linked` source reaches it (sources_for_reason):
    the cause has its source and there is nothing left to search for. An unstated
    task stays — it is precisely the row a found-but-never-said source hangs on.
    A plan closed before task reasons existed has none, and so no slot.

    In the order the tasks were recorded, cut to `slot_cap()`. Empty — never an
    error — when the plan recorded no task and when nothing dates the plan.
    """
    from . import reasons
    tasks = [r for r in (conn.execute("SELECT * FROM change_reason_v WHERE id = ?", (t["id"],)).fetchone()
                         for t in reasons._live_task_reasons(conn, plan_id))]
    window = plan_window(conn, plan_id)
    if not tasks or window is None:
        return []
    root = _root_anchor(conn, plan_id)
    covered = _covered(conn, plan_id, psg_db_path, [t["id"] for t in tasks])
    root_task = None
    if root and root["root_plan_id"] != plan_id:
        first = reasons._live_task_reasons(conn, root["root_plan_id"])
        root_task = first[0]["id"] if first else None
    out = []
    for t in map(dict, tasks):
        sources = sources_for_reason(conn, t["id"])
        if any(s["verifiability"] == "linked" for s in sources):
            continue
        attach_to = ({"utterance_id": root["utterance_id"]} if root and root["utterance_id"]
                     else {"reason_id": root_task} if root_task else {"reason_id": t["id"]})
        text = _task_text(conn, t)
        out.append({
            "reason_id": t["id"], "plan_id": plan_id, "tier": t["tier"], "task": text,
            "nodes": covered.get(t["id"], []),
            "root": root,
            "window": list(window),
            "hints": task_hints(covered.get(t["id"], []), root["words"] if root else None, text),
            "sources": sources,
            "attach_to": attach_to,
            "attach_with": _attach_with(attach_to),
            "log_with": (f"provledger review evidence-log --plan {plan_id} --reason {t['id']} "
                         "--outcome attached|found_nothing|timed_out|not_searched [--tool-hint \"<which tool>\"]"),
        })
    cap = slot_cap() if limit is None else int(limit)
    return out[:max(cap, 0)]


# ── the log: what became of each slot ────────────────────────────────────────

# The four values change_reason_v computes. Kept here as a list to validate
# against, never to recompute: the view owns what a level means.
EVIDENCE_LEVELS = ("linked", "verbal", "task_context", "unstated")
TIERS = ("stated", "asserted", "derived", "unstated")


def record(conn, *, plan_id: str, outcome: str, node_key: str | None = None, reason_id: int | None = None,
           reason_tier: str | None = None, evidence_level: str | None = None, searched: bool | None = None,
           tool_hint: str | None = None, elapsed_ms: int | None = None, commit: bool = True) -> int:
    """Append one row: this slot ended this way.

    A slot is a task reason since step 3b (`reason_id`); a row from before names
    the changed node (`node_key`). One of the two is required. A task slot's tier
    defaults to its reason's. `searched` follows from the outcome and does not
    have to be passed; passing one that disagrees is refused rather than
    reconciled, because the two columns exist to check each other. There is no
    `at` parameter — the time is the database's reading.
    """
    if node_key is None and reason_id is None:
        raise ValueError("an evidence_log row names the slot: node_key or reason_id")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    if reason_id is not None:
        row = conn.execute("SELECT tier FROM change_reason WHERE id = ?", (int(reason_id),)).fetchone()
        if row is None:
            raise ValueError(f"there is no reason {reason_id} in this ledger")
        reason_tier = reason_tier or row[0]
    if reason_tier is not None and reason_tier not in TIERS:
        raise ValueError(f"reason_tier must be one of {TIERS}, got {reason_tier!r}")
    if evidence_level is not None and evidence_level not in EVIDENCE_LEVELS:
        raise ValueError(f"evidence_level must be one of {EVIDENCE_LEVELS}, got {evidence_level!r}")
    if elapsed_ms is not None and int(elapsed_ms) < 0:
        raise ValueError("elapsed_ms counts forwards and cannot be negative")
    entered = outcome != "not_searched"
    if searched is not None and bool(searched) != entered:
        raise ValueError(f"searched={bool(searched)} contradicts outcome {outcome!r}: "
                         "only 'not_searched' never entered the search")
    cur = conn.execute(
        "INSERT INTO evidence_log (plan_id, node_key, reason_id, reason_tier, evidence_level, searched, tool_hint, "
        "outcome, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (plan_id, node_key, None if reason_id is None else int(reason_id), reason_tier, evidence_level,
         1 if entered else 0, tool_hint, outcome, None if elapsed_ms is None else int(elapsed_ms)))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def log_for_plan(conn, plan_id: str) -> list[dict]:
    """Every recorded ending of every slot of one plan, oldest first."""
    return [dict(r) for r in conn.execute("SELECT * FROM evidence_log WHERE plan_id = ? ORDER BY id", (plan_id,))]
