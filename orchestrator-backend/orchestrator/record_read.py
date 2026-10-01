"""record_read — `provledger record <cite>`: one record, whole and uncut.

Every read in the product truncates, and until now nothing could undo it.
`context_pack._slim` cuts at 240 characters, mid-word, so on the live ledger
`#274` (516 characters) ended at "…which auto-fires r" and `#790` (994) at
"…api's regis". `provledger reason` offers only `mark`, which is a WRITE. The
end of a recorded sentence was therefore unreachable by any read at all, and in
one evaluation an agent dropped a piece of evidence from a reply for exactly
that reason: it could not read the end of it.

This is the bottom of the one navigation path §10.2 asks for. Whatever a `why`
line or a `graph` row points at, this opens in full — under the cite forms the
product already prints rather than a spelling invented here:

    #12   a ledger record (a reason, a constraint, a rejected path)
    #r3   a source (a `reference` row: the email, the meeting, the doc)

A bare `12` or `r3` works too, because a shell eats `#` in some places and
refusing a cite over a character the reader could not keep would be petty.
Anything else raises rather than guessing which of the two was meant.

**What is shown, and what that does NOT change.** A record's own text,
including a personal `rationale`, is printed here with its visibility named.
That is not a hole in E1: `export` still refuses to let a personal row travel,
by code, and this is a local read by the person who owns the ledger. Naming the
visibility is the honest middle — the reader learns what may not be forwarded,
instead of finding a blank where a sentence used to be.

Read-only, strictly. `why` writes a `read_hit` for every record it shows
(deliberate, and under review as FL-150); this must not, because then the act
of reading a row whole would change what the ledger says about how often it was
read. SELECTs only, no counters, no model call.
"""
from __future__ import annotations

import re

CITE = re.compile(r"^#?(?P<source>r)?(?P<id>\d+)$", re.IGNORECASE)
RECORD_COLUMNS = ("id", "project", "node_key", "run_id", "event_id", "plan_id", "step_id", "kind", "role",
                  "verbatim_utterance_id", "verbatim_start", "verbatim_end", "interpretation", "statement",
                  "rationale", "rationale_visibility", "statement_visibility", "state", "superseded_by",
                  "review_after", "occurred_at", "recorded_at", "recorded_by", "tier", "rule_id",
                  "evidence_level", "significance_eff", "utterance_origin")
LEVEL_LABELS = {"linked": "linked", "verbal": "verbal", "task_context": "task context", "unstated": "unstated"}


_DEVIATION_CITE = re.compile(r"^#?v(?P<id>\d+)$")


def cite_error(cite: str) -> str:
    """What to say about a cite this read cannot take.

    A refusal that only lists what IS accepted leaves a reader holding a token
    nothing reads. `#v94` is the live case: `plan` prints deviations that way (a
    bare `#94` used to collide with a ledger record and answer with the wrong
    row), and a reader handed one tries `record` first, because that is the read
    for cites. So the refusal names where it lives instead."""
    text = (cite or "").strip()
    if _DEVIATION_CITE.match(text):
        return (f"not a record: {text} is a deviation, which lives on its plan — "
                f"`provledger plan <plan-id>` prints it with the justification in full")
    return f"not a cite: {cite!r} — use `#12` for a ledger record or `#r3` for a source"


def parse_cite(cite: str) -> tuple[str, int]:
    """('record'|'source', id) from `#12` / `12` / `#r3` / `r3`."""
    m = CITE.match((cite or "").strip())
    if not m:
        raise ValueError(cite_error(cite))
    return ("source" if m.group("source") else "record"), int(m.group("id"))


def record(conn, cite: str) -> dict | None:
    """One record or one source, in full. None when the id names nothing — an
    id nobody wrote is not an empty record."""
    kind, rid = parse_cite(cite)
    return _source(conn, rid) if kind == "source" else _reason(conn, rid)


# ── a ledger record ──────────────────────────────────────────────────────────

def _reason(conn, rid: int) -> dict | None:
    row = conn.execute(f"SELECT {', '.join(RECORD_COLUMNS)} FROM change_reason_v WHERE id = ?", (rid,)).fetchone()
    if row is None:
        return None
    doc: dict = dict(zip(RECORD_COLUMNS, row))
    doc["cite"] = f"#{rid}"
    # `kind` is already a column on both tables (technical/… and doc/email/…), so the
    # discriminator gets its own name rather than shadowing a recorded value.
    doc["cite_kind"] = "record"
    doc["significance"] = doc.pop("significance_eff")
    doc["utterance"] = _utterance(conn, doc)
    # The text, assembled the same way every other read assembles it — and then
    # NOT cut. interpretation first, then a recorded statement, then the quoted
    # span of the utterance for a `stated` row that has no words of its own.
    doc["text"] = doc.get("interpretation") or doc.get("statement") or (doc["utterance"] or {}).get("quoted") or ""
    doc["text_chars"] = len(doc["text"])
    doc["references"] = [{"cite": f"#r{r[0]}", "kind": r[1], "label": r[2], "uri": r[3], "stance": r[4],
                          "verifiability": r[5], "last_checked": r[6]} for r in conn.execute(
        "SELECT f.id, f.kind, f.label, f.uri, l.stance, f.verifiability, f.last_checked "
        "FROM reference_link l JOIN reference f ON f.id = l.reference_id "
        "WHERE l.reason_id = ? ORDER BY f.id", (rid,))]
    doc["shown"] = conn.execute("SELECT COUNT(*) FROM read_hit WHERE reason_id = ?", (rid,)).fetchone()[0]
    doc["shown_by_moment"] = {r[0]: r[1] for r in conn.execute(
        "SELECT moment, COUNT(*) FROM read_hit WHERE reason_id = ? GROUP BY 1", (rid,))}
    doc["adopted_by"] = [r[0] or (f"session {r[1]}" if r[1] else "?") for r in conn.execute(
        "SELECT DISTINCT plan_id, session_id FROM influence WHERE reason_id = ? ORDER BY plan_id", (rid,))]
    doc["supersedes"] = [r[0] for r in conn.execute("SELECT id FROM change_reason WHERE superseded_by = ? ORDER BY id", (rid,))]
    doc["node_history"] = f"provledger why {doc['node_key']}" if doc.get("node_key") else None
    return doc


def _utterance(conn, doc: dict) -> dict | None:
    """The words the record quotes, and the words around them.

    The span is what the record claims; the whole utterance is what was
    actually said. Printing only the span would leave a reader unable to check
    that the quotation is fair, which is the one thing a `stated` tier asserts."""
    uid = doc.get("verbatim_utterance_id")
    if not uid:
        return None
    row = conn.execute("SELECT id, session_id, project, plan_id, text, occurred_at, recorded_at, visibility, origin "
                       "FROM utterance WHERE id = ?", (uid,)).fetchone()
    if row is None:
        return {"id": uid, "missing": True}
    lo, hi = doc.get("verbatim_start"), doc.get("verbatim_end")
    text = row[4] or ""
    return {"id": row[0], "session_id": row[1], "project": row[2], "plan_id": row[3], "text": text,
            "occurred_at": row[5], "recorded_at": row[6], "visibility": row[7], "origin": row[8],
            "span": [lo, hi] if lo is not None and hi is not None else None,
            "quoted": text[lo:hi] if lo is not None and hi is not None else None}


# ── a source ─────────────────────────────────────────────────────────────────

def _source(conn, rid: int) -> dict | None:
    row = conn.execute("SELECT id, project, kind, uri, label, occurred_at, registered_at, verifiability, "
                       "visibility, last_checked FROM reference WHERE id = ?", (rid,)).fetchone()
    if row is None:
        return None
    cols = ("id", "project", "kind", "uri", "label", "occurred_at", "registered_at", "verifiability",
            "visibility", "last_checked")
    doc: dict = dict(zip(cols, row))
    doc["cite"] = f"#r{rid}"
    doc["cite_kind"] = "source"
    # Append-only by design: a link that went dead stays dead on that date, and
    # a later `ok` is another row beside it, never a correction of it. So every
    # check is printed, oldest first, and none of them overwrites another.
    doc["checks"] = [{"checked_at": r[0], "verdict": r[1], "note": r[2]} for r in conn.execute(
        "SELECT checked_at, verdict, note FROM reference_check WHERE reference_id = ? ORDER BY id", (rid,))]
    doc["anchors"] = [{"cite": (f"#{r[0]}" if r[0] is not None else f"utterance #{r[1]}"), "stance": r[2]}
                      for r in conn.execute("SELECT reason_id, utterance_id, stance FROM reference_link "
                                            "WHERE reference_id = ? ORDER BY reason_id, utterance_id", (rid,))]
    return doc


# ── rendering ────────────────────────────────────────────────────────────────

_FOOTER = ("── how to go on\n"
           "   provledger record #<id> | #r<id>   another record, or the source behind this one\n"
           "   provledger why <node>              that node's history, bounded\n"
           "   provledger graph <node> --depth 1  what sits next to it in the graph")

# The plan id is printed three lines up, but as a label. This makes it a door, and
# with the real id rather than a placeholder — a reader sent to look up something
# already on their screen usually does not. It is the hop to what a record cannot
# hold: what the step actually ran, what failed, what was measured and decided.
_FOOTER_PLAN = "   provledger plan {plan_id}{pad}  the task that produced it: its steps, failures and logs"


def _footer(doc: dict) -> str:
    plan_id = doc.get("plan_id")
    if not plan_id:
        return _FOOTER
    # keep the two-column shape of the lines above it
    pad = " " * max(1, 33 - len("provledger plan " + str(plan_id)))
    return _FOOTER + "\n" + _FOOTER_PLAN.format(plan_id=plan_id, pad=pad)


def render(doc: dict | None, cite: str | None = None) -> str:
    if doc is None:
        return f"{cite or '(cite)'}: no record and no source with that id in the ledger"
    return _render_source(doc) if doc.get("cite_kind") == "source" else _render_record(doc)


def _render_record(doc: dict) -> str:
    level = LEVEL_LABELS.get(doc.get("evidence_level"), doc.get("evidence_level") or "?")
    lines = [f"{doc['cite']} · {doc['role']} · {doc['tier']} · source level {level} · {doc['kind']}"
             + (f" · {doc['state']}" if doc.get("state") != "active" else "")
             + (f" · significance {doc['significance']}" if doc.get("significance") else ""),
             f"   {doc['node_key'] or '(no node — a floating rejected path)'}"
             + (f" · {doc['node_history']}" if doc.get("node_history") else ""),
             f"   plan {doc['plan_id']}" + (f" · step {doc['step_id']}" if doc.get("step_id") else "")
             + (f" · rule {doc['rule_id']}" if doc.get("rule_id") else ""),
             f"   happened {doc['occurred_at']} · recorded {doc['recorded_at']} by {doc['recorded_by']}"
             + (f" · review after {doc['review_after']}" if doc.get("review_after") else ""),
             f"   shown {doc['shown']}"
             + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(doc["shown_by_moment"].items())) + ")" if doc["shown_by_moment"] else "")
             + (" · adopted by " + ", ".join(doc["adopted_by"]) if doc["adopted_by"] else " · not adopted"),
             ""]
    if doc.get("superseded_by"):
        lines.append(f"── superseded by #{doc['superseded_by']} (this row's own words are unchanged; nothing was rewritten)")
    if doc.get("supersedes"):
        lines.append("── supersedes " + ", ".join(f"#{i}" for i in doc["supersedes"]))
    lines += [f"── text · {doc['text_chars']} characters, whole", f"   {doc['text'] or '(no words: an unstated slot)'}"]
    if doc.get("rationale"):
        lines += ["", f"── rationale ({doc.get('rationale_visibility')}) · this half is not what `export` lets travel",
                  f"   {doc['rationale']}"]
    q = doc.get("utterance")
    if q:
        lines += ["", f"── quotes utterance #{q['id']}"
                  + (f" · {q.get('origin')}" if q.get("origin") else "")
                  + (f" · said {q.get('occurred_at')}" if q.get("occurred_at") else "")
                  + (f" · {q.get('visibility')}" if q.get("visibility") else "")]
        if q.get("span"):
            lines.append(f"   quoted span [{q['span'][0]}:{q['span'][1]}]: {q.get('quoted')}")
        if q.get("text"):
            lines.append(f"   said in full: {q['text']}")
    if doc["references"]:
        lines += ["", f"── sources · {len(doc['references'])}"]
        # contradicts before supports (ACC-16): counter-evidence is not a footnote
        for r in sorted(doc["references"], key=lambda r: (r["stance"] != "contradicts", r["cite"])):
            lines.append(f"   {r['cite']} · {r['stance']} · {r['kind']} · {r['label']}"
                         + (f" · {r['uri']}" if r.get("uri") else "")
                         + f" · {r['verifiability']}"
                         + (f" · last checked {r['last_checked']}" if r.get("last_checked") else " · never checked"))
    else:
        lines += ["", "── sources · none hang on this record"]
    lines += ["", _footer(doc)]
    return "\n".join(lines)


def _render_source(doc: dict) -> str:
    lines = [f"{doc['cite']} · source · {doc['kind']} · {doc['verifiability']} · {doc.get('visibility')}",
             f"   {doc['label']}" + (f" · {doc['uri']}" if doc.get("uri") else " · (no uri)"),
             f"   happened {doc['occurred_at']} · registered {doc['registered_at']}"
             + (f" · last checked {doc['last_checked']}" if doc.get("last_checked") else " · never checked"),
             ""]
    if doc["checks"]:
        lines.append(f"── checks · {len(doc['checks'])} · append-only: a later verdict sits beside the earlier one, never over it")
        for c in doc["checks"]:
            lines.append(f"   {c['checked_at']} · {c['verdict']}" + (f" · {c['note']}" if c.get("note") else ""))
    else:
        lines.append("── checks · none: nobody has opened this pointer yet")
    lines.append("")
    if doc["anchors"]:
        lines.append(f"── hangs on · {len(doc['anchors'])}")
        for a in doc["anchors"]:
            lines.append(f"   {a['cite']} · {a['stance']}")
    else:
        lines.append("── hangs on · nothing yet")
    lines += ["", _footer(doc)]
    return "\n".join(lines)
