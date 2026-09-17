"""checks — the two-layer pre-change check and the plan headline (DP phase 2,
Task 3; spec §5.2, D1, I1–I4, I10, I11).

Layer self (time & intent): what the target's own history says — an active
constraint anchored on it, a rejected path, a prior plan whose claim about it
failed, an upstream node that was removed. Layer impact (space): what the
blast radius says — consumers that eat its output, unverified upstream
tables, constraints anchored one hop downstream.

A headline is the findings of both layers plus a one-line summary. It is
computed at publish, printed, stored (append-only), and it NEVER blocks: an
unanswered blocking finding is counted and highlighted, publish exits 0.
The one exception is a human-recorded constraint whose extensions entry says
`block: true` (`hard`): then publish exits 5 until someone responds.

An agent answers a finding with `respond()` (revise | proceed + rationale);
every record it cites — and the record behind the finding — becomes an
`influence(via=headline_response)`. Showing a record in a headline writes
`read_hit` (the pack did that); adopting it is only ever a response (I11).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from . import db, provenance

KINDS_SELF = ("active_constraint", "rejected_path", "prior_outcome_failed", "removed_upstream")
KINDS_IMPACT = ("downstream_break", "unverified_upstream", "downstream_constraint")
KINDS_ASSERTED = ("similar_intent",)
SEVERITIES = ("blocking", "warning", "info")


@dataclass
class Finding:
    id: str
    layer: str                      # self | impact
    kind: str
    tier: str                       # observed | derived | stated | asserted
    severity: str                   # blocking | warning | info
    text: str
    anchor: str                     # the target qualified name
    evidence: dict = field(default_factory=dict)      # reason_id / event_id / expectation_id / consumers
    hard: bool = False              # a human constraint with block: true → publish may not proceed unanswered
    response: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _fid(kind: str, anchor_key: str | None, n: int) -> str:
    return f"{kind}:{(anchor_key or 'none')[:12]}:{n}"



# ── DP phase 2d (Task 0): a finding carries the words recorded at the time ────
# "upstream X was removed in run N" is true and useless: the agent cannot see WHY
# it went, so it cannot adopt that record either. Every observation-shaped finding
# now appends the reason recorded against that node for that run (or that plan),
# the user's own words first, and carries its reason_id so a response can cite it.
# Nothing recorded reads "nothing was stated at the time" — the gap is said
# out loud, never left blank.
REASON_SNIPPET_MAX = 160


def _reason_at(conn, node_key: str | None, *, run_id: int | None = None, plan_id: str | None = None) -> dict | None:
    """The live reason recorded on `node_key` for that run / plan: the user's own
    words win over an agent's reading, the newest wins over the older. None when
    there is nothing on record (or no connection to ask)."""
    if conn is None or not node_key or (run_id is None and plan_id is None):
        return None
    # The ordering is the honesty rule: a reason recorded FOR this run (or this
    # plan) beats a looser one, the user's own words beat an agent's reading, and
    # the newest beats the older. The fallback to "any live reason on this node"
    # matters because `provledger note` — how a PERSON records a sentence — writes
    # no run_id, so a strict run/plan match would print "nothing was stated at the
    # time" for exactly the case this feature exists for. The finding always names
    # the plan the words were recorded in, so a reader can see how contemporaneous
    # they are.
    params: list = [node_key]
    rank = "0"
    if run_id is not None and plan_id is not None:
        rank = "CASE WHEN r.run_id = ? OR r.plan_id = ? THEN 0 ELSE 1 END"
        params += [int(run_id), plan_id]
    elif run_id is not None:
        rank = "CASE WHEN r.run_id = ? THEN 0 ELSE 1 END"
        params.append(int(run_id))
    elif plan_id is not None:
        rank = "CASE WHEN r.plan_id = ? THEN 0 ELSE 1 END"
        params.append(plan_id)
    try:
        row = conn.execute(
            "SELECT r.id, r.plan_id, r.tier, r.rule_id, r.recorded_by, "
            "       COALESCE(substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start), r.interpretation, r.statement) AS text "
            "FROM change_reason_v r LEFT JOIN utterance u ON u.id = r.verbatim_utterance_id "
            "WHERE r.node_key = ? AND r.role = 'reason' AND r.state = 'active' AND r.superseded_by IS NULL "
            f"ORDER BY {rank}, (r.tier = 'stated') DESC, r.id DESC LIMIT 1", params).fetchone()
    except Exception:                      # an older DB has no change_reason_v: say nothing, invent nothing
        return None
    if not row or not row["text"]:
        return None
    return {"reason_id": row["id"], "plan_id": row["plan_id"], "tier": row["tier"],
            "label": "the user's own words" if row["tier"] == "stated" else (row["rule_id"] or row["recorded_by"]),
            "text": row["text"][:REASON_SNIPPET_MAX]}


def _because(rec: dict | None, *, asked: bool) -> str:
    """The clause appended to a finding. `asked` is False when the caller passed
    no connection — then the finding says nothing about reasons at all, rather
    than claiming none exists."""
    if not asked:
        return ""
    if not rec:
        return " — nothing was stated at the time"
    return f' — because: "{rec["text"]}" ({rec["label"]}, {rec["plan_id"]})'


def layer_self(pack, *, hard_statements: frozenset = frozenset(), conn=None) -> list[Finding]:
    out: list[Finding] = []
    for t in pack.targets:
        key = t.node_key or t.qualified_name
        for n, c in enumerate(t.constraints, 1):
            hard = c["recorded_by"] == "human" and (c.get("text") or "") in hard_statements
            out.append(Finding(_fid("active_constraint", key, n), "self", "active_constraint",
                               "stated" if c["tier"] == "stated" else "asserted", "blocking",
                               f"{c['text']} ({c['recorded_by']}, {c['occurred_at'][:10]})", t.qualified_name,
                               {"reason_id": c["id"]}, hard=hard))
        for n, r in enumerate(t.rejected_paths, 1):
            out.append(Finding(_fid("rejected_path", key, n), "self", "rejected_path",
                               "derived" if r["tier"] == "derived" else "asserted", "warning",
                               f"\"{(r['text'] or '')[:120]}\" ({r.get('rule_id') or r['recorded_by']}, {r['plan_id']})",
                               t.qualified_name, {"reason_id": r["id"]}))
        for n, o in enumerate(t.prior_outcomes, 1):
            failed = (o["kind"] == "survival" and o.get("signal") not in (None, "untouched", "untouched_consumed", "survived")) \
                or (o["kind"] == "observed" and isinstance(o.get("delta_pct"), (int, float)) and abs(o["delta_pct"]) > 5)
            if failed:
                rec = _reason_at(conn, t.node_key or key, plan_id=o["plan_id"])
                ev_dict = {"expectation_id": o["expectation_id"]}
                if rec:
                    ev_dict.update(reason_id=rec["reason_id"], plan_id=rec["plan_id"])
                out.append(Finding(_fid("prior_outcome_failed", key, n), "self", "prior_outcome_failed", "derived", "warning",
                                   f"{o['plan_id']} claimed \"{o['claim']}\" — outcome {o['kind']} {o.get('signal') or o.get('delta_pct')}"
                                   + _because(rec, asked=conn is not None),
                                   t.qualified_name, ev_dict))
        for n, ev in enumerate(getattr(t, "removed_upstream", []) or [], 1):
            rec = _reason_at(conn, ev.get("node_key"), run_id=ev.get("run_id"))
            ev_dict = {"event_id": ev["event_id"]}
            if rec:
                ev_dict.update(reason_id=rec["reason_id"], plan_id=rec["plan_id"])
            out.append(Finding(_fid("removed_upstream", key, n), "self", "removed_upstream", "observed", "blocking",
                               f"upstream {ev['qualified_name']} was removed in run {ev['run_id']}"
                               + _because(rec, asked=conn is not None),
                               t.qualified_name, ev_dict))
    return out


def layer_impact(pack, *, hard_statements: frozenset = frozenset()) -> list[Finding]:
    out: list[Finding] = []
    for t in pack.targets:
        key = t.node_key or t.qualified_name
        if t.output_consumers:
            out.append(Finding(_fid("downstream_break", key, 1), "impact", "downstream_break", "derived", "warning",
                               f"{len(t.output_consumers)} consumer(s) eat its output: {', '.join(t.output_consumers[:4])}"
                               + (" …" if len(t.output_consumers) > 4 else ""), t.qualified_name, {"consumers": list(t.output_consumers)}))
        if t.upstream_assumptions:
            out.append(Finding(_fid("unverified_upstream", key, 1), "impact", "unverified_upstream", "derived", "info",
                               "reads " + ", ".join(u["table"] for u in t.upstream_assumptions[:4]) + " — schema assumed, not verified at plan time",
                               t.qualified_name, {"tables": [u["table"] for u in t.upstream_assumptions]}))
        for nb in t.output_consumers:
            entry = pack.neighbors_counts.get(nb) or {}
            for n, c in enumerate(entry.get("statements") or [], 1):
                hard = c["recorded_by"] == "human" and (c.get("text") or "") in hard_statements
                out.append(Finding(_fid("downstream_constraint", nb, n), "impact", "downstream_constraint",
                                   "stated" if c["tier"] == "stated" else "asserted", "blocking",
                                   f"downstream {nb}: {c['text']} ({c['recorded_by']})", t.qualified_name, {"reason_id": c["id"]}, hard=hard))
            if entry.get("constraints") and not entry.get("statements"):
                out.append(Finding(_fid("downstream_constraint", nb, 0), "impact", "downstream_constraint", "derived", "warning",
                                   f"downstream {nb} carries {entry['constraints']} active constraint(s) — `provledger why {nb}`",
                                   t.qualified_name, {"count": entry["constraints"]}))
    return out


def asserted_notes(conn, notes, targets: list[str]) -> list[Finding]:
    """The agent's own findings (similar_intent): warning, asserted, and every
    cited reason_id must exist (I3) — otherwise the note is refused."""
    out = []
    for n, note in enumerate(notes or [], 1):
        kind = note.get("finding_kind") or note.get("kind")
        if kind not in KINDS_ASSERTED:
            raise ValueError(f"headline_notes[{n - 1}]: kind must be one of {KINDS_ASSERTED}")
        cites = [int(x) for x in (note.get("cites") or [])]
        if not cites:
            raise ValueError(f"headline_notes[{n - 1}]: an asserted finding must cite at least one reason_id")
        for rid in cites:
            if provenance.get_reason(conn, rid) is None:
                raise ValueError(f"headline_notes[{n - 1}]: cited reason {rid} does not exist")
        out.append(Finding(_fid(kind, targets[0] if targets else None, n), "self", kind, "asserted", "warning",
                           str(note.get("text") or "")[:240], targets[0] if targets else "", {"reason_ids": cites}))
    return out


def summarize(findings: list[Finding], n_targets: int, shown: int, adopted: int = 0) -> dict:
    return {"targets": n_targets, "layers": 2, "findings": len(findings),
            "blocking": sum(1 for f in findings if f.severity == "blocking"),
            "warning": sum(1 for f in findings if f.severity == "warning"),
            "info": sum(1 for f in findings if f.severity == "info"),
            "unanswered": sum(1 for f in findings if f.severity == "blocking" and f.response is None),
            "hard_unanswered": sum(1 for f in findings if f.hard and f.response is None),
            "shown": shown, "adopted": adopted}


def headline(conn, *, project: str, pack, plan_id: str | None = None, session_id: str | None = None,
             notes=(), hard_statements: frozenset = frozenset(), commit: bool = True) -> dict:
    """Compute, store (a new headline row every time) and return the headline. Never empty (I1)."""
    findings = layer_self(pack, hard_statements=hard_statements, conn=conn) + layer_impact(pack, hard_statements=hard_statements)
    findings += asserted_notes(conn, notes, [t.qualified_name for t in pack.targets])
    doc = {"findings": [f.as_dict() for f in findings],
           "summary": summarize(findings, len(pack.targets), pack.shown),
           "hints": list(getattr(pack, "hints", []) or []), "generated_at": pack.generated_at}
    cur = conn.execute("INSERT INTO headline (project, plan_id, session_id, findings_json) VALUES (?, ?, ?, ?)",
                       (project, plan_id, session_id, json.dumps(doc, ensure_ascii=False, default=str)))
    doc["headline_id"] = int(cur.lastrowid)
    if plan_id:
        try:
            conn.execute("UPDATE Plans SET headline_json = ? WHERE plan_id = ?", (json.dumps(doc, ensure_ascii=False, default=str), plan_id))
        except Exception:
            pass
    if commit:
        conn.commit()
    return doc


def latest(conn, *, plan_id: str | None = None, session_id: str | None = None) -> dict | None:
    if plan_id:
        r = conn.execute("SELECT id, findings_json FROM headline WHERE plan_id = ? ORDER BY id DESC LIMIT 1", (plan_id,)).fetchone()
    else:
        r = conn.execute("SELECT id, findings_json FROM headline WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
    if not r:
        return None
    doc = json.loads(r[1])
    doc["headline_id"] = r[0]
    resp = {x[0]: {"action": x[1], "rationale": x[2], "by": x[3], "cites": json.loads(x[4] or "[]")}
            for x in conn.execute("SELECT finding_id, action, rationale, by, cites_json FROM headline_response WHERE headline_id = ?", (r[0],))}
    for f in doc["findings"]:
        f["response"] = resp.get(f["id"])
    fs = [Finding(**{k: v for k, v in f.items() if k in Finding.__dataclass_fields__}) for f in doc["findings"]]
    doc["summary"] = summarize(fs, doc["summary"].get("targets", 0), doc["summary"].get("shown", 0),
                               adopted=conn.execute("SELECT COUNT(*) FROM influence WHERE plan_id = ? AND via = 'headline_response'", (plan_id,)).fetchone()[0] if plan_id else 0)
    return doc


def respond(conn, *, plan_id: str, finding_id: str, action: str, rationale: str | None, by: str, cites=(), commit: bool = True) -> int:
    """Answer one finding of the plan's latest headline. The response is a
    row; the finding's own record and every cited record become influence
    (via headline_response, by) — that is what "adopted" means (I4, I10)."""
    if action not in ("revise", "proceed") or by not in ("agent", "human"):
        raise ValueError("action is revise | proceed and by is agent | human")
    doc = latest(conn, plan_id=plan_id)
    if doc is None:
        raise ValueError(f"plan {plan_id} has no headline")
    finding = next((f for f in doc["findings"] if f["id"] == finding_id), None)
    if finding is None:
        raise ValueError(f"no finding {finding_id!r} in the latest headline of {plan_id}")
    cites = [int(x) for x in cites or ()]
    for rid in cites:
        if provenance.get_reason(conn, rid) is None:
            raise ValueError(f"cited reason {rid} does not exist")
    cur = conn.execute("INSERT INTO headline_response (headline_id, finding_id, action, rationale, by, cites_json) VALUES (?, ?, ?, ?, ?, ?)",
                       (doc["headline_id"], finding_id, action, rationale, by, json.dumps(cites)))
    plan = db.get_plan(conn, plan_id) or {}
    project = plan.get("project") or conn.execute("SELECT project FROM headline WHERE id = ?", (doc["headline_id"],)).fetchone()[0]
    ids = set(cites)
    ev = finding.get("evidence") or {}
    if ev.get("reason_id"):
        ids.add(int(ev["reason_id"]))
    ids |= {int(x) for x in ev.get("reason_ids") or []}
    for rid in sorted(ids):
        conn.execute("INSERT INTO influence (reason_id, project, plan_id, node_key, via, by) VALUES (?, ?, ?, ?, 'headline_response', ?)",
                     (rid, project, plan_id, finding.get("anchor"), by))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def close_headline(conn, *, plan_id: str, commit: bool = False) -> dict:
    """At close: every blocking finding that was proceeded past or never
    answered gets an expectation (channel survival) so the outcome of going
    past it is tracked like any other claim (§5.2)."""
    doc = latest(conn, plan_id=plan_id)
    if doc is None:
        return {"expectations": 0, "unanswered": 0, "proceeded": 0}
    plan = db.get_plan(conn, plan_id) or {}
    project = plan.get("project")
    n = unanswered = proceeded = 0
    for f in doc["findings"]:
        if f["severity"] != "blocking":
            continue
        resp = f.get("response")
        if resp is None:
            unanswered += 1
            how = "unanswered"
        elif resp["action"] == "proceed":
            proceeded += 1
            how = "proceeded past"
        else:
            continue
        if not project:
            continue
        claim = f"{how} #{f['id']} and no step of this plan failed afterwards"
        exists = conn.execute("SELECT 1 FROM expectations WHERE plan_id = ? AND claim = ?", (plan_id, claim)).fetchone()
        if exists:
            continue
        conn.execute("INSERT INTO expectations (plan_id, step_id, project, target, target_kind, claim, channel) VALUES (?, NULL, ?, ?, 'node', ?, 'survival')",
                     (plan_id, project, f.get("anchor") or "-", claim))
        n += 1
    if commit:
        conn.commit()
    return {"expectations": n, "unanswered": unanswered, "proceeded": proceeded}


def render(doc: dict, width: int = 60) -> str:
    """The terminal shape (human-readable); the same text goes to the plan row."""
    s = doc["summary"]
    lines = [f"── plan headline · {s['targets']} targets · 2 layers " + "─" * max(4, width - 30)]
    mark = {"blocking": "⚠", "warning": "·", "info": "·"}
    for f in doc["findings"]:
        resp = f.get("response")
        tail = (f" → {resp['action']} ({resp['by']})" if resp else ("  → respond: headline-respond" if f["severity"] == "blocking" else ""))
        hard = " [block]" if f.get("hard") else ""
        lines.append(f" {mark.get(f['severity'], '·')} {f['severity']:<9}{hard} {f['id']:<32} {f['text']}{tail}")
    if not doc["findings"]:
        lines.append(" 0 findings — the two layers checked every target and found nothing on record")
    lines.append(f" {s['unanswered']} findings unanswered · {s['shown']} records shown · {s.get('adopted', 0)} adopted")
    for h in doc.get("hints") or []:
        lines.append(f" {h}")
    return "\n".join(lines)
