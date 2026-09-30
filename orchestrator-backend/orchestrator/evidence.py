"""evidence — A6: which changed data points are worth looking for evidence for,
and what became of each look.

Two halves, and provLedger does no searching in either.

`slots_for_plan` is the to-check list: one entry per changed node of the plan that
has no checkable source yet, carrying the node, what is recorded about the change,
the plan's own time window, the identifiers to search on and how significant the
change is. The host agent takes that list to its own mailbox, chat and tracker
with its own credentials, and whatever it finds comes back through
`provledger reference add`. Nothing here opens a link, and nothing here asks a
model anything: the hints are identifiers pulled out of the graph and out of the
words already recorded, by rule.

`record` is the other half — the audit trail. Every slot's ending is written to
`evidence_log`, so "why is this one blank" has an answer: nobody searched, or
somebody searched and found nothing, or the search ran out of time. A system that
degrades in silence is the thing this product exists to prevent, so the silence
is the one thing that is not allowed to go unrecorded.

Two cost gates, both from A6: only the top `PROVLEDGER_EVIDENCE_SLOTS` (default 5)
slots by significance enter a search, and the window is the plan's own span rather
than the project's history.
"""
from __future__ import annotations

import os
import re

from . import psg_bridge, significance

DEFAULT_SLOTS = 5
SLOTS_ENV = "PROVLEDGER_EVIDENCE_SLOTS"

# What the host may be told about a slot's ending. Three of them are the reason a
# row can be blank, and they are kept apart on purpose: `not_searched` never
# entered the search (no tool, or the cap cut it off), `found_nothing` searched
# the window and the window held nothing, `timed_out` started and ran out of time.
# Collapsing them into one "no evidence" is how a degradation becomes invisible.
OUTCOMES = ("attached", "found_nothing", "timed_out", "not_searched")
WHAT_CHANGED_CHARS = 400
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


def hints_for(qualified_name: str | None, what_changed: str = "") -> list[str]:
    """The identifiers to search on: the node's names, then the identifier-shaped
    words of what was recorded about the change. Deterministic, order stable,
    de-duplicated case-insensitively on first spelling."""
    out: list[str] = []
    seen: set[str] = set()

    def add(word: str) -> None:
        low = word.lower()
        if low not in seen:
            seen.add(low)
            out.append(word)

    for part in _name_parts(qualified_name):
        add(part)
    for token in _IDENT_RE.findall(what_changed or ""):
        if _identifier_like(token):
            add(token)
    return out[:MAX_HINTS]


# ── the window: this plan, not this project ──────────────────────────────────

def _now(conn) -> str:
    return conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now')").fetchone()[0]


def plan_window(conn, plan_id: str) -> list[str] | None:
    """[start, end] of the plan's own span, or None when nothing dates it.

    A plan still open ends its window now rather than at the end of time: the
    window bounds the host agent's search, and an unbounded end bounds nothing.
    `session:<sid>` takes its span from session_run, the same as triggers does.
    """
    row = conn.execute("SELECT created_at, completed_at FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    if row is None and str(plan_id).startswith("session:"):
        row = conn.execute("SELECT started_at, ended_at FROM session_run WHERE session_id = ?",
                           (str(plan_id).split(":", 1)[1],)).fetchone()
    if row is None or not row[0]:
        return None
    return [row[0], row[1] or _now(conn)]


# ── the slots ────────────────────────────────────────────────────────────────

def _reason_rows(conn, plan_id: str) -> dict[str, list[dict]]:
    """The plan's active `reason` rows per node, oldest first."""
    out: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT id, node_key, tier, evidence_level, interpretation, statement, "
                          "verbatim_utterance_id, verbatim_start, verbatim_end "
                          "FROM change_reason_v WHERE plan_id = ? AND role = 'reason' AND node_key IS NOT NULL "
                          "AND superseded_by IS NULL ORDER BY id", (plan_id,)):
        out.setdefault(r["node_key"], []).append(dict(r))
    return out


def _reason_text(conn, reason: dict) -> str | None:
    """What the reason says, in the words it was recorded in — the span of the
    user's own sentence when there is one, otherwise the recorded reading. Never
    a paraphrase: this string is shown to the user beside the pointer."""
    if reason.get("verbatim_utterance_id"):
        row = conn.execute("SELECT substr(text, ? + 1, ? - ?) FROM utterance WHERE id = ?",
                           (reason["verbatim_start"], reason["verbatim_end"], reason["verbatim_start"],
                            reason["verbatim_utterance_id"])).fetchone()
        if row and row[0]:
            return str(row[0]).strip()[:WHAT_CHANGED_CHARS]
    for col in ("interpretation", "statement"):
        if reason.get(col):
            return str(reason[col]).strip()[:WHAT_CHANGED_CHARS]
    return None


def _graph_phrase(node: dict) -> str:
    """What the graph saw, said plainly, for a node nobody has explained yet. It
    describes the event and stops there — inventing prose about why would be the
    guess this whole feature refuses."""
    return f"{'/'.join(node.get('event_types') or ['changed'])} · {node.get('qualified_name') or node['node_key']}" \
           + (f" ({node['node_type']})" if node.get("node_type") else "")


def slots_for_plan(conn, *, project: str, plan_id: str, psg_db_path: str | None = None,
                   limit: int | None = None) -> list[dict]:
    """The changed nodes of this plan that still have no checkable source.

    A node drops out as soon as one of its reasons carries a `linked` pointer:
    the evidence is there and there is nothing left to search for. A node stays
    in whatever tier its reason landed at — including `unstated`, which is
    precisely the row the found-but-never-said evidence hangs on.

    Descending significance, cut to `slot_cap()`. Empty — never an error — when
    the plan changed nothing, when there is no graph, and when nothing dates the
    plan: an empty list is a true answer and an exception is not.
    """
    changed = psg_bridge.changed_node_keys(psg_db_path, plan_id)
    if not changed:
        return []
    window = plan_window(conn, plan_id)
    if window is None:
        return []
    by_node = _reason_rows(conn, plan_id)
    out = []
    for node in changed:
        rows = by_node.get(node["node_key"]) or []
        if any(r["evidence_level"] == "linked" for r in rows):
            continue
        reason = rows[-1] if rows else None
        text = _reason_text(conn, reason) if reason else None
        fired = significance.signals(conn, {"project": project, "plan_id": plan_id, "node_key": node["node_key"],
                                            "run_id": node.get("run_id"),
                                            "tier": reason["tier"] if reason else None}, psg_db_path)
        out.append({
            "node": node.get("qualified_name") or node["node_key"],
            "node_key": node["node_key"],
            "node_type": node.get("node_type"),
            "event_types": list(node.get("event_types") or []),
            "run_id": node.get("run_id"),
            "reason_id": reason["id"] if reason else None,
            "reason_tier": reason["tier"] if reason else None,
            "evidence_level": reason["evidence_level"] if reason else None,
            "what_changed": text or _graph_phrase(node),
            "window": list(window),
            "hints": hints_for(node.get("qualified_name") or node["node_key"], text or ""),
            # the share of significance.py's six signals that fired: one ranking,
            # taken from the one place that scores a change, so the cap below cuts
            # the least significant slots and not an arbitrary tail
            "significance": round(len(fired) / significance.SIGNAL_COUNT, 4),
            "significance_level": "major" if fired else "minor",
            "significance_basis": "; ".join(fired) if fired else significance.NO_SIGNAL_BASIS,
        })
    out.sort(key=lambda s: (-s["significance"], s["node_key"]))
    cap = slot_cap() if limit is None else int(limit)
    return out[:max(cap, 0)]


# ── the log: what became of each slot ────────────────────────────────────────

# The four values change_reason_v computes. Kept here as a list to validate
# against, never to recompute: the view owns what a level means.
EVIDENCE_LEVELS = ("linked", "verbal", "task_context", "unstated")
TIERS = ("stated", "asserted", "derived", "unstated")


def record(conn, *, plan_id: str, node_key: str, outcome: str, reason_tier: str | None = None,
           evidence_level: str | None = None, searched: bool | None = None, tool_hint: str | None = None,
           elapsed_ms: int | None = None, commit: bool = True) -> int:
    """Append one row: this slot ended this way.

    `searched` follows from the outcome and does not have to be passed; passing
    one that disagrees is refused rather than reconciled, because the two columns
    exist to check each other. There is no `at` parameter — the time is the
    database's reading, like every other recorded_at in this schema.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
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
        "INSERT INTO evidence_log (plan_id, node_key, reason_tier, evidence_level, searched, tool_hint, "
        "outcome, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (plan_id, node_key, reason_tier, evidence_level, 1 if entered else 0, tool_hint, outcome,
         None if elapsed_ms is None else int(elapsed_ms)))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def log_for_plan(conn, plan_id: str) -> list[dict]:
    """Every recorded ending of every slot of one plan, oldest first."""
    return [dict(r) for r in conn.execute("SELECT * FROM evidence_log WHERE plan_id = ? ORDER BY id", (plan_id,))]
