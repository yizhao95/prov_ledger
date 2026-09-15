"""constraints — close-time check that anchored constraints were READ.

Spec §2.9. A constraint (LedgerEntries.kind='constraint') is anchored to
data points by node_key. At plan time impact_preflight surfaces every
constraint anchored to a declared target and records the ids the author
saw (Plans.impact_context.constraint_ids). At close, every constraint
anchored to a node the plan actually changed or removed that was NOT among
those ids is recorded as `constraint_bypassed` — a node_reason of kind
constraint_ref (source system, tier derived) plus a review-log line. Never
blocks: the point is that the bypass is visible, not that it is prevented.

The backend cannot import skills, so anchored_constraints is the twin of
ledger_store.constraints_for (same SQL, tested on both sides).
"""
from __future__ import annotations

import json

from . import db, provenance, psg_bridge, telemetry


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("subjects", "keywords"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else []
        except ValueError:
            d[k] = []
    return d


def anchored_constraints(conn, project: str, node_keys, qualified_names=()) -> list[dict]:
    """Active constraints whose subjects contain ANY of `node_keys` or
    `qualified_names` — exact anchor match (phase 5: a declared constraint may
    anchor by qualified name / owner.column); a restricted entry comes back
    with rationale=None."""
    keys = [k for k in (*(node_keys or []), *(qualified_names or ())) if k]
    if not keys:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT l.* FROM LedgerEntries l, json_each(l.subjects) s "
            "WHERE l.project = ? AND l.kind = 'constraint' AND l.status = 'active' "
            f"AND s.value IN ({','.join('?' * len(keys))}) ORDER BY l.id",
            (project, *keys)).fetchall()
    except Exception:   # sqlite3.OperationalError: orchestrator DB predates migration 015
        return []
    out = []
    for r in rows:
        d = _row_to_dict(r)
        if d.get("why_visibility") == "restricted":
            d["rationale"] = None
        out.append(d)
    return out


def bypassed_at_close(conn, *, project: str, plan_id: str, psg_db_path: str | None,
                      review_step_id: str, commit: bool = False) -> int:
    """For every constraint anchored to a node this plan changed/removed that
    the plan's impact_context did NOT surface, record it — visibly — and
    continue. Returns the number of (constraint, node) pairs recorded."""
    changed = psg_bridge.changed_node_keys(psg_db_path, plan_id)
    keys = [c["node_key"] for c in changed]
    qn_of = {c["node_key"]: c.get("qualified_name") for c in changed}
    hit = anchored_constraints(conn, project, keys, qualified_names=[q for q in qn_of.values() if q])
    if not hit:
        return 0
    plan = db.get_plan(conn, plan_id) or {}
    try:
        seen = set(json.loads(plan.get("impact_context") or "{}").get("constraint_ids", []))
    except ValueError:
        seen = set()
    already = {r[0] for r in conn.execute(
        "SELECT interpretation FROM change_reason WHERE plan_id = ? AND role = 'constraint' AND rule_id = 'constraint_bypassed'", (plan_id,))}
    already |= {r["text"] for r in db.get_node_reasons(conn, plan_id=plan_id) if r["kind"] == "constraint_ref"}
    run_id = max((c["run_id"] for c in changed), default=None)
    n = 0
    for c in hit:
        if c["id"] in seen:
            continue
        text = f"constraint_bypassed:{c['id']}: {c['statement']}"
        for k in keys:
            if (k in c["subjects"] or qn_of.get(k) in c["subjects"]) and text not in already:
                # DP phase 1: a derived constraint row (rule constraint_bypassed) in change_reason;
                # node_reason_v shows it with the old shape (kind constraint_ref, tier derived)
                provenance.insert_reason(conn, project=project, plan_id=plan_id, node_key=k, kind="organizational",
                                         role="constraint", run_id=run_id, interpretation=text,
                                         rule_id="constraint_bypassed", recorded_by="system", commit=False)
                already.add(text)
                n += 1
    if n and commit:
        conn.commit()
    if n:
        telemetry.append_step_log(
            conn, review_step_id,
            f"[CONSTRAINT BYPASSED] {n} anchored constraint(s) were changed without being read at plan time "
            f"(ids not in impact_context.constraint_ids)", commit=commit)
    return n
