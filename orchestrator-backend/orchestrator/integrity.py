"""integrity — the three chains, and the git anchors they must agree with
(DP phase 3, Task 0; spec §7, G1/G2).

`provenance.verify_chain` already answers "has any row moved since it was
written". That answer is self-referential: a ledger that walks its own chains
proves only that it is internally consistent with itself *right now*. This
module adds the second half — an **external witness**. When a plan closes, the
three chain heads are written into `git notes --ref provledger` on HEAD. The
note lives in the repository, not in the database, so rewriting the ledger no
longer rewrites its own evidence.

What that buys, stated narrowly because the narrow claim is the true one:

    it shows that these records existed at the moment of that commit and have
    not been altered since. It does not show that what they say happened.

The chain definition itself (`provenance.canonical` / `chain_hash` /
`_UNCHAINED`) is **read here and never touched**: changing it would break every
row already written.

Design notes:
- one compact JSON object per line, because `git notes append` concatenates
  messages with a blank line and a multi-line payload would not survive that;
- the head hash of a chain at row N folds in every predecessor, so comparing
  one hash per table per anchor checks the whole prefix before that anchor;
- notes are append-only: `anchor_heads` only ever calls `git notes append`, and
  a line that this module did not write is counted (`unreadable`), never
  crashed on and never silently dropped.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

from . import provenance

# the three tables spec §7 names. `declared_node` also carries a chain
# (migration 026); it is verifiable through provenance.verify_chain, but the
# anchor payload keeps the shape the spec wrote down.
CHAINS = ("utterance", "reference", "change_reason")
NOTES_REF = "provledger"
PAYLOAD_VERSION = 1

# What this module claims, and what it does not — quoted by the card and the docs.
CLAIM = ("These records existed at the anchored commit and have not been altered since. "
         "That is not a claim that what they say happened.")


class AnchorError(RuntimeError):
    """git could not be asked, or refused. Callers at close time log it and go on
    — an anchor that could not be written must never block a plan from closing."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(repo, *args, check: bool = True) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=30)
    except FileNotFoundError as e:
        raise AnchorError("git is not on PATH") from e
    except OSError as e:
        raise AnchorError(f"git could not run in {repo}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise AnchorError(f"git timed out: {' '.join(args)}") from e
    if check and p.returncode != 0:
        raise AnchorError(f"git {' '.join(args)} failed ({p.returncode}): {(p.stderr or '').strip()}")
    return p


def _head(repo) -> str:
    p = _git(repo, "rev-parse", "HEAD", check=False)
    if p.returncode != 0:
        raise AnchorError(f"no HEAD in {repo}: {(p.stderr or '').strip()}")
    return p.stdout.strip()


# ── chain heads ───────────────────────────────────────────────────────────────

def chain_heads(conn) -> dict:
    """{table: {id, hash, rows}} — what an anchor pins down. An empty chain says
    `id: None`, not `id: 0`: there is nothing to anchor yet, and that is a fact."""
    out: dict = {}
    for table in CHAINS:
        row = conn.execute(f"SELECT id, hash FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        out[table] = {"id": (row[0] if row else None), "hash": (row[1] if row else None), "rows": int(n)}
    return out


def anchor_payload(conn, *, plan_id: str | None = None) -> dict:
    """The JSON that goes into the note: the three heads, when, and which plan
    closed. Nothing about the rows themselves — a note is a witness, not a copy."""
    heads = chain_heads(conn)
    payload: dict = {t: {"id": heads[t]["id"], "hash": heads[t]["hash"]} for t in CHAINS}
    payload["at"] = _now()
    payload["plan_id"] = plan_id
    payload["v"] = PAYLOAD_VERSION
    return payload


def payload_line(payload: dict) -> str:
    """One line, always — `git notes append` joins messages with a blank line,
    so a payload that wrapped would come back unparseable."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ── writing and reading the anchors ───────────────────────────────────────────

def pins_something(payload: dict) -> bool:
    """True when the payload names at least one row. A note full of nulls is not
    an anchor: it vouches for nothing, and it is indistinguishable on the ref
    from one that does."""
    return any((payload.get(t) or {}).get("id") is not None for t in CHAINS)


def anchor_heads(repo, payload: dict, *, ref: str = NOTES_REF) -> str:
    """Append one anchor to `refs/notes/<ref>` on HEAD and return the note blob
    sha. Append-only: the previous anchors on that commit stay where they are.
    Raises AnchorError; the caller decides whether that is fatal (at close time
    it never is)."""
    if repo is None:
        raise AnchorError("no repository to anchor in")
    if not os.path.isdir(str(repo)):
        raise AnchorError(f"not a directory: {repo}")
    if not pins_something(payload):
        raise AnchorError("nothing to anchor: the three chains are empty, so a note would pin no row")
    # A directory INSIDE a work tree is not that work tree: `git notes` run
    # there writes to the enclosing repository. That is how the phantom-uplift
    # e2e suite, which registers `examples/phantom-uplift`, appended ten empty
    # notes to the developer's own checkout.
    top = _git(repo, "rev-parse", "--show-toplevel", check=False)
    root = top.stdout.strip()
    if top.returncode != 0 or not root:
        raise AnchorError(f"{repo} is not inside a git work tree")
    if os.path.realpath(root) != os.path.realpath(str(repo)):
        raise AnchorError(f"{repo} is not the root of its git work tree ({root}) — "
                          f"the note would land on {root}, which nobody registered")
    head = _head(repo)
    _git(repo, "notes", "--ref", ref, "append", head, "-m", payload_line(payload))
    return note_sha(repo, head, ref=ref) or ""


def head_commit(repo) -> str:
    """The commit an anchor written right now would hang on."""
    return _head(repo)


def note_sha(repo, commit: str, *, ref: str = NOTES_REF) -> str | None:
    """The sha of the note blob currently attached to `commit`, or None."""
    p = _git(repo, "notes", "--ref", ref, "list", commit, check=False)
    if p.returncode != 0:
        return None
    return (p.stdout.strip().split() or [None])[0]


def read_anchors(repo, *, ref: str = NOTES_REF) -> list[dict]:
    """Every anchor in the notes ref, oldest first by its own `at`.

    Each entry is the payload plus `commit` and `note_sha`. A line this module
    did not write is returned as `{"unreadable": <line>, ...}` rather than
    dropped — a note somebody else appended is information, not noise.
    """
    if repo is None or not os.path.isdir(str(repo)):
        return []
    p = _git(repo, "notes", "--ref", ref, "list", check=False)
    if p.returncode != 0:
        return []
    out: list[dict] = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        blob, commit = parts
        shown = _git(repo, "notes", "--ref", ref, "show", commit, check=False)
        if shown.returncode != 0:
            continue
        for text in shown.stdout.splitlines():
            text = text.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                out.append({"unreadable": text, "commit": commit, "note_sha": blob})
                continue
            if not isinstance(payload, dict) or payload.get("v") != PAYLOAD_VERSION:
                out.append({"unreadable": text, "commit": commit, "note_sha": blob})
                continue
            entry = dict(payload)
            if not pins_something(payload):
                # a witness that names no row; counted, never deleted, and never
                # passed off as an anchor
                entry.update({"empty": True, "commit": commit, "note_sha": blob})
                out.append(entry)
                continue
            entry["commit"] = commit
            entry["note_sha"] = blob
            out.append(entry)
    out.sort(key=lambda a: (a.get("at") or "", a.get("commit") or ""))
    return out


# ── the report ────────────────────────────────────────────────────────────────

def _match(conn, anchor: dict) -> dict | None:
    """The first table whose anchored head hash is not what the ledger holds."""
    for table in CHAINS:
        pinned = anchor.get(table) or {}
        rid, anchored_hash = pinned.get("id"), pinned.get("hash")
        if rid is None or anchored_hash is None:
            continue                                     # the chain was empty when the note was written
        row = conn.execute(f"SELECT hash FROM {table} WHERE id = ?", (rid,)).fetchone()
        found = row[0] if row else None
        if found != anchored_hash:
            return {"table": table, "id": rid, "anchored_hash": anchored_hash, "found_hash": found,
                    "commit": anchor.get("commit"), "note_sha": anchor.get("note_sha"),
                    "at": anchor.get("at"), "plan_id": anchor.get("plan_id")}
    return None


def verify(conn, *, against_notes: bool = False, repo=None, ref: str = NOTES_REF) -> dict:
    """Walk the three chains; optionally check them against the git anchors.

    `ok` is the whole verdict: every chain walks AND (when asked) every anchor
    still describes this ledger. `anchored` is a separate word — a ledger with
    no anchor is internally fine, it simply has no outside witness, and
    `anchors.reason` says why there is none.
    """
    tables: dict = {}
    heads = chain_heads(conn)
    ok = True
    for table in CHAINS:
        res = provenance.verify_chain(conn, table)
        tables[table] = {"ok": res["ok"], "rows": res["rows"], "first_bad_id": res["first_bad_id"],
                         "head_id": heads[table]["id"], "head_hash": heads[table]["hash"]}
        if not res["ok"]:
            ok = False
    anchors: dict = {"found": 0, "matched": 0, "unreadable": 0, "empty": 0, "first_mismatch": None, "latest": None,
                     "reason": "not checked (--against-notes was not asked for)"}
    report = {"ok": ok, "tables": tables, "anchored": False, "anchors": anchors,
              "repo": (str(repo) if repo else None), "notes_ref": ref, "checked_at": _now(), "claim": CLAIM}
    if not against_notes:
        return report
    try:
        entries = read_anchors(repo, ref=ref)
    except AnchorError as e:
        anchors["reason"] = f"git unavailable: {e}"
        return report
    if repo is None:
        anchors["reason"] = "no repository given: pass --repo, or run inside a registered project"
        return report
    if not os.path.isdir(os.path.join(str(repo), ".git")) and not entries:
        anchors["reason"] = f"no git repository at {repo}"
        return report
    good = [a for a in entries if "unreadable" not in a and not a.get("empty")]
    anchors["unreadable"] = sum(1 for a in entries if "unreadable" in a)
    anchors["empty"] = sum(1 for a in entries if a.get("empty"))
    anchors["found"] = len(good)
    if not good:
        anchors["reason"] = (f"no note on refs/notes/{ref} pins a row"
                             + (f" ({anchors['empty']} line(s) pin nothing)" if anchors["empty"] else
                                ": nothing has been anchored in this repository yet"))
        return report
    anchors["reason"] = None
    matched = 0
    for a in good:
        mm = _match(conn, a)
        if mm is None:
            matched += 1
        elif anchors["first_mismatch"] is None:
            anchors["first_mismatch"] = mm
    anchors["matched"] = matched
    latest = good[-1]
    anchors["latest"] = {"note_sha": latest.get("note_sha"), "commit": latest.get("commit"),
                         "at": latest.get("at"), "plan_id": latest.get("plan_id"),
                         "heads": {t: latest.get(t) for t in CHAINS}}
    report["anchored"] = matched > 0
    if anchors["first_mismatch"] is not None:
        report["ok"] = False
    return report


# ── rendering ─────────────────────────────────────────────────────────────────

def anchor_line(report: dict) -> str:
    """One line for a human: the newest anchor, or why there is none."""
    a = report["anchors"]
    if a["found"] == 0:
        return f"anchors: 0 anchor(s) — not anchored ({a['reason']})"
    latest = a["latest"] or {}
    tail = (f"anchors: {a['found']} anchor(s), {a['matched']} matched · latest note "
            f"{(latest.get('note_sha') or '')[:12]} @ {(latest.get('commit') or '')[:12]} "
            f"({latest.get('at') or '-'}, plan {latest.get('plan_id') or '-'})")
    if a["unreadable"]:
        tail += f" · {a['unreadable']} line(s) on the ref were not written by provledger"
    if a.get("empty"):
        tail += f" · {a['empty']} line(s) on the ref pin nothing"
    mm = a["first_mismatch"]
    if mm:
        tail += (f"\n  ** anchor mismatch: {mm['table']} #{mm['id']} was anchored as "
                 f"{(mm['anchored_hash'] or '')[:12]} but the ledger holds "
                 f"{(mm['found_hash'] or 'nothing')[:12]} (note {(mm['note_sha'] or '')[:12]} @ "
                 f"{(mm['commit'] or '')[:12]}) **")
    return tail


def render(report: dict) -> str:
    lines = [f"provledger verify · {report['checked_at']}"]
    for table in CHAINS:
        t = report["tables"][table]
        state = "ok" if t["ok"] else f"chain broken at #{t['first_bad_id']}"
        head = (t["head_hash"] or "")[:12] or "-"
        lines.append(f"  chain {table}: {state} · {t['rows']} row(s) walked · head #{t['head_id'] or '-'} {head}")
    lines.append("  " + anchor_line(report))
    lines.append(f"  {CLAIM}")
    return "\n".join(lines)
