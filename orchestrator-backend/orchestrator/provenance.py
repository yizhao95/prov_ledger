"""provenance — the store layer of decision provenance (DP phase 1, Task 2).

Three append-only tables (utterance / reference / change_reason) and the one
rule the whole feature rests on: **the source level of a reason is decided
here, from what the row points at — never by the caller.**

  verbatim=(utterance_id, start, end)   → tier 'stated'   (the span must exist in that utterance)
  rule_id                               → tier 'derived'
  interpretation / statement            → tier 'asserted' (a reading of something, not the words)
  nothing                               → tier 'unstated' (recorded as a gap, never invented)

insert_reason has no `tier` and no `recorded_at` parameter: bare text can at
most become asserted (A2/A4), and the record time is the database's clock (B5).
Every table keeps a hash chain (sha256 of the canonical row + the previous
row's hash); verify_chain() walks it and names the first row that no longer
matches (G1). The only UPDATEs the triggers allow are change_reason.superseded_by
(supersede()) and reference.last_checked.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from . import db

TIERS = ("stated", "asserted", "derived", "unstated")
KINDS = ("technical", "organizational", "mixed")
ROLES = ("reason", "rejected_path", "constraint")
RECORDED_BY = ("human", "agent", "system")
REFERENCE_KINDS = ("email", "meeting", "chat", "ticket", "doc", "commit", "verbal", "other")
# the two columns the triggers let change stay outside the chain, so the chain
# still verifies after supersede() / a last_checked update
_UNCHAINED = ("id", "hash", "prev_hash", "superseded_by", "last_checked")


def canonical(row: dict) -> str:
    """Sorted-key JSON of the row without id / hash / prev_hash and the two mutable columns."""
    return json.dumps({k: v for k, v in row.items() if k not in _UNCHAINED}, sort_keys=True, default=str,
                      ensure_ascii=False, separators=(",", ":"))


def chain_hash(prev_hash: str | None, row: dict) -> str:
    return hashlib.sha256((canonical(row) + (prev_hash or "")).encode("utf-8")).hexdigest()


def _last_hash(conn, table: str) -> str | None:
    r = conn.execute(f"SELECT hash FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    return r[0] if r else None


def _db_now(conn) -> str:
    """The database's own clock in its own format — callers never pass it."""
    return conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now')").fetchone()[0]


def _insert_chained(conn, table: str, row: dict) -> int:
    prev = _last_hash(conn, table)
    row = dict(row, prev_hash=prev)
    row["hash"] = chain_hash(prev, row)
    cols = ", ".join(row)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    return int(cur.lastrowid)


# ── utterance ─────────────────────────────────────────────────────────────────

def insert_utterance(conn, *, session_id: str, project: str | None, plan_id: str | None, text: str,
                     occurred_at: str, visibility: str = "personal", commit: bool = True) -> int:
    if not text:
        raise ValueError("an utterance needs text")
    row = {"session_id": session_id, "project": project, "plan_id": plan_id, "text": text, "occurred_at": occurred_at,
           "recorded_at": _db_now(conn), "visibility": visibility}
    rid = _insert_chained(conn, "utterance", row)
    if commit:
        conn.commit()
    return rid


def get_utterance(conn, utterance_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM utterance WHERE id = ?", (utterance_id,)).fetchone()
    return dict(r) if r else None


# ── reference ─────────────────────────────────────────────────────────────────

def insert_reference(conn, *, project: str, kind: str, label: str, occurred_at: str, uri: str | None = None,
                     visibility: str = "shareable", commit: bool = True) -> int:
    """verifiability is decided here: verbal → 'verbal' (no uri by definition),
    a uri → 'linked', otherwise 'unreachable' until someone finds the link."""
    if kind not in REFERENCE_KINDS:
        raise ValueError(f"reference kind must be one of {REFERENCE_KINDS}, got {kind!r}")
    if kind == "verbal" and uri:
        raise ValueError("a verbal reference has no uri")
    if not label or len(label) > 512:
        raise ValueError("a reference label is 1..512 characters (a pointer, not a body)")
    verifiability = "verbal" if kind == "verbal" else ("linked" if uri else "unreachable")
    row = {"project": project, "kind": kind, "uri": uri, "label": label, "occurred_at": occurred_at,
           "registered_at": _db_now(conn), "verifiability": verifiability, "visibility": visibility, "last_checked": None}
    rid = _insert_chained(conn, "reference", row)
    if commit:
        conn.commit()
    return rid


def link_reference(conn, reason_id: int, reference_id: int, commit: bool = True) -> None:
    conn.execute("INSERT OR IGNORE INTO reference_link (reason_id, reference_id) VALUES (?, ?)", (reason_id, reference_id))
    if commit:
        conn.commit()


# ── change_reason ─────────────────────────────────────────────────────────────

def derive_tier(*, verbatim, rule_id, interpretation, statement) -> str:
    if verbatim is not None:
        return "stated"
    if rule_id:
        return "derived"
    if interpretation or statement:
        return "asserted"
    return "unstated"


def _check_span(conn, verbatim) -> tuple[int, int, int]:
    try:
        utterance_id, start, end = verbatim
    except (TypeError, ValueError):
        raise ValueError("verbatim must be (utterance_id, start, end)") from None
    u = get_utterance(conn, int(utterance_id))
    if u is None:
        raise ValueError(f"utterance {utterance_id} does not exist")
    start, end = int(start), int(end)
    if not (0 <= start < end <= len(u["text"])):
        raise ValueError(f"span [{start}, {end}) is outside utterance {utterance_id} (length {len(u['text'])})")
    return int(utterance_id), start, end


def insert_reason(conn, *, project: str, plan_id: str, node_key: str | None, kind: str, role: str = "reason",
                  occurred_at: str | None = None, step_id: str | None = None, run_id: int | None = None,
                  event_id: int | None = None, verbatim: tuple[int, int, int] | None = None,
                  interpretation: str | None = None, statement: str | None = None, rationale: str | None = None,
                  refs=(), rule_id: str | None = None, recorded_by: str = "agent", significance: str | None = None,
                  rationale_visibility: str = "personal", statement_visibility: str = "shareable",
                  state: str = "active", commit: bool = True) -> int:
    """Append one reason. The tier is derived from the inputs (see module doc);
    there is deliberately no way to pass it, and no way to pass recorded_at."""
    if kind not in KINDS or role not in ROLES or recorded_by not in RECORDED_BY:
        raise ValueError(f"bad kind/role/recorded_by {kind!r}/{role!r}/{recorded_by!r}")
    if node_key is None and role != "rejected_path":
        raise ValueError("a reason needs a node_key (only a rejected_path may float)")
    tier = derive_tier(verbatim=verbatim, rule_id=rule_id, interpretation=interpretation, statement=statement)
    if tier == "unstated" and rationale:
        raise ValueError("a rationale needs a statement or an interpretation to hang on")
    vid = vs = ve = None
    if verbatim is not None:
        vid, vs, ve = _check_span(conn, verbatim)
    row = {"project": project, "node_key": node_key, "run_id": run_id, "event_id": event_id, "plan_id": plan_id,
           "step_id": step_id, "kind": kind, "role": role, "verbatim_utterance_id": vid, "verbatim_start": vs,
           "verbatim_end": ve, "interpretation": interpretation, "statement": statement, "rationale": rationale,
           "rationale_visibility": rationale_visibility, "statement_visibility": statement_visibility,
           "state": state, "superseded_by": None, "review_after": None,
           "occurred_at": occurred_at or _db_now(conn), "recorded_at": _db_now(conn), "recorded_by": recorded_by,
           "tier": tier, "rule_id": rule_id, "significance": significance}
    rid = _insert_chained(conn, "change_reason", row)
    for ref in refs or ():
        link_reference(conn, rid, int(ref), commit=False)
    if commit:
        conn.commit()
    return rid


def get_reason(conn, reason_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM change_reason_v WHERE id = ?", (reason_id,)).fetchone()
    return dict(r) if r else None


def reasons_for_plan(conn, plan_id: str, role: str | None = None) -> list[dict]:
    sql = "SELECT * FROM change_reason_v WHERE plan_id = ?"
    args: list = [plan_id]
    if role:
        sql += " AND role = ?"
        args.append(role)
    return [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]


def reasons_for_node(conn, node_key: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM change_reason_v WHERE node_key = ? ORDER BY id", (node_key,))]


def supersede(conn, old_id: int, new_id: int, commit: bool = True) -> None:
    """The one allowed UPDATE: the old row points at its successor; its own
    state and text stay exactly as recorded."""
    if old_id == new_id:
        raise ValueError("a reason cannot supersede itself")
    if get_reason(conn, new_id) is None:
        raise ValueError(f"reason {new_id} does not exist")
    conn.execute("UPDATE change_reason SET superseded_by = ? WHERE id = ?", (new_id, old_id))
    if commit:
        conn.commit()


# ── integrity ─────────────────────────────────────────────────────────────────

def verify_chain(conn, table: str) -> dict:
    """Recompute every row's hash from its stored columns and the previous
    row's hash. {ok, rows, first_bad_id}: the first row whose hash or prev_hash
    does not match — a tampered row, or a row inserted around the store."""
    if table not in ("utterance", "reference", "change_reason", "declared_node"):
        raise ValueError(f"no hash chain on {table!r}")
    prev: str | None = None
    n = 0
    for r in conn.execute(f"SELECT * FROM {table} ORDER BY id"):
        n += 1
        row = dict(r)
        if row["prev_hash"] != prev or chain_hash(prev, row) != row["hash"]:
            return {"ok": False, "rows": n, "first_bad_id": row["id"]}
        prev = row["hash"]
    return {"ok": True, "rows": n, "first_bad_id": None}
