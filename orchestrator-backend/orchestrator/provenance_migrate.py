"""provenance_migrate — the legacy reasons and constraints become change_reason
rows WITHOUT being rewritten (DP phase 1, Task 7).

node_reason (phase 3–8) called every agent sentence tier=stated. Nothing in
that table is the user's words, so nothing there can be stated: each row
becomes an `asserted` change_reason (interpretation = the old text) — or
`unstated` when the text was NULL, or `derived` with rule_id 'legacy' when
the old tier was derived — and one trigger_log row (rule_id 'reclass') says
where it came from. LedgerEntries.kind='constraint' rows become
role='constraint' change_reasons (statement + rationale; a restricted
rationale stays personal) with their why_ref registered as an unreachable
doc reference. The old tables are read, never UPDATEd or DELETEd; run() is
idempotent (migration_state['dp_reclass']) and db.open_db runs it once after
the SQL migrations.
"""
from __future__ import annotations

import json

from . import provenance

STATE_KEY = "dp_reclass"


def status(conn) -> dict | None:
    r = conn.execute("SELECT value, at FROM migration_state WHERE key = ?", (STATE_KEY,)).fetchone()
    return {"value": r[0], "at": r[1]} if r else None


def _first_subject(subjects) -> str | None:
    try:
        subs = json.loads(subjects) if isinstance(subjects, str) else (subjects or [])
    except ValueError:
        subs = []
    subs = [s for s in subs if isinstance(s, str) and s]
    keyed = [s for s in subs if s.startswith("nk_")]
    return (keyed or subs or [None])[0]


def _subjects(subjects) -> list[str]:
    try:
        subs = json.loads(subjects) if isinstance(subjects, str) else (subjects or [])
    except ValueError:
        subs = []
    return [s for s in subs if isinstance(s, str) and s]


def run(conn) -> dict:
    """Copy node_reason and the constraint ledger into change_reason once.
    Returns the counts; {"skipped": True} when already done."""
    if status(conn) is not None:
        return {"skipped": True, **status(conn)}
    out = {"reasons": 0, "rejected_paths": 0, "constraint_refs": 0, "constraints": 0, "references": 0, "by_tier": {}}
    # ── node_reason ──
    for r in conn.execute("SELECT * FROM node_reason ORDER BY id"):
        r = dict(r)
        recorded_by = r["source"] if r["source"] in provenance.RECORDED_BY else "system"
        role = "rejected_path" if r["kind"] == "rejected_path" else "reason"
        if r["kind"] == "constraint_ref":
            role = "constraint"
        text = r["text"]
        kw = dict(project=r["project"], plan_id=r["plan_id"], node_key=r["node_key"], kind="technical", role=role,
                  step_id=r.get("step_id"), run_id=r.get("run_id"), occurred_at=r.get("created_at"),
                  recorded_by=recorded_by, commit=False)
        if role == "constraint":
            kw.update(kind="organizational", statement=text, rule_id="legacy")
        elif not text:
            pass                                       # the old backstop rows: unstated, no rule
        elif r["tier"] == "derived":
            kw.update(rule_id="legacy", interpretation=text)
        else:
            kw.update(interpretation=text)             # stated / asserted by an agent or a person: asserted
        if role == "rejected_path" and r["node_key"] is None and not text:
            continue                                   # nothing to carry
        try:
            new_id = provenance.insert_reason(conn, **kw)
        except ValueError:
            continue                                   # an unanchored non-rejected row cannot exist in change_reason
        tier = provenance.get_reason(conn, new_id)["tier"]
        out["by_tier"][tier] = out["by_tier"].get(tier, 0) + 1
        out["rejected_paths" if role == "rejected_path" else ("constraint_refs" if role == "constraint" else "reasons")] += 1
        conn.execute("INSERT INTO trigger_log (project, plan_id, node_key, path, rule_id, verdict, basis) VALUES (?, ?, ?, 'code', 'reclass', 'auto', ?)",
                     (r["project"], r["plan_id"], r["node_key"], f"migrated from node_reason#{r['id']} tier={r['tier']}"))
    # ── LedgerEntries.constraint ──
    try:
        rows = [dict(x) for x in conn.execute("SELECT * FROM LedgerEntries WHERE kind = 'constraint' ORDER BY id")]
    except Exception:
        rows = []
    for e in rows:
        subjects = _subjects(e.get("subjects"))
        anchor = _first_subject(e.get("subjects"))
        if anchor is None:
            continue
        refs = []
        if e.get("why_ref"):
            refs.append(provenance.insert_reference(conn, project=e["project"], kind="doc", label=str(e["why_ref"])[:512],
                                                    occurred_at=e.get("created_at") or "1970-01-01 00:00:00", commit=False))
            out["references"] += 1
        state = "active" if e.get("status", "active") == "active" else "superseded"
        for subject in subjects:                       # one row per anchor so every subject still matches
            provenance.insert_reason(conn, project=e["project"], plan_id=e.get("plan_id") or "ledger", node_key=subject,
                                     kind="organizational", role="constraint", statement=e["statement"],
                                     rationale=e.get("rationale") or None,
                                     rationale_visibility="personal" if e.get("why_visibility") == "restricted" else "shareable",
                                     refs=refs, recorded_by="human", occurred_at=e.get("created_at"), state=state, commit=False)
            out["constraints"] += 1
        conn.execute("INSERT INTO trigger_log (project, plan_id, node_key, path, rule_id, verdict, basis) VALUES (?, ?, ?, 'code', 'reclass', 'auto', ?)",
                     (e["project"], e.get("plan_id") or "ledger", anchor, f"migrated from LedgerEntries#{e['id']} constraint"))
    conn.execute("INSERT INTO migration_state (key, value, at) VALUES (?, 'done', strftime('%Y-%m-%d %H:%M:%S','now'))", (STATE_KEY,))
    conn.commit()
    return out
