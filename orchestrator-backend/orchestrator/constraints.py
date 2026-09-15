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
    """Active constraints anchored on ANY of `node_keys` / `qualified_names` —
    since DP phase 1 read from change_reason (role constraint, state active,
    not superseded; one row per anchor, deduplicated by statement); the old
    LedgerEntries rows were migrated in. A row whose rationale is personal
    comes back with rationale=None and only its why_ref (the first linked
    reference's label)."""
    keys = [k for k in (*(node_keys or []), *(qualified_names or ())) if k]
    if not keys:
        return []
    try:
        rows = conn.execute(
            "SELECT r.id, r.node_key, r.plan_id, r.statement, r.rationale, r.rationale_visibility, r.state, r.recorded_by, "
            "       r.occurred_at, r.recorded_at, r.evidence_level, "
            "       (SELECT f.label FROM reference_link l JOIN reference f ON f.id = l.reference_id WHERE l.reason_id = r.id ORDER BY f.id LIMIT 1) AS why_ref "
            f"FROM change_reason_v r WHERE r.project = ? AND r.role = 'constraint' AND r.state = 'active' AND r.superseded_by IS NULL "
            f"AND r.rule_id IS NULL AND r.node_key IN ({','.join('?' * len(keys))}) ORDER BY r.id",
            (project, *keys)).fetchall()
    except Exception:   # sqlite3.OperationalError: orchestrator DB predates migration 018
        return []
    out, seen = [], set()
    for r in rows:
        d = dict(r)
        twin = (d["statement"], d["rationale"], d["rationale_visibility"], d["why_ref"], d["plan_id"], d["recorded_at"])
        if twin in seen:                       # the same constraint recorded once per anchored subject
            continue
        seen.add(twin)
        # every anchor of this constraint (the twin rows), not only the ones that matched
        d["subjects"] = [x[0] for x in conn.execute(
            "SELECT node_key FROM change_reason WHERE project = ? AND role = 'constraint' AND statement IS ? AND rationale IS ? "
            "AND rationale_visibility = ? AND plan_id = ? AND recorded_at = ? AND state = 'active' AND superseded_by IS NULL ORDER BY id",
            (project, d["statement"], d["rationale"] if d["rationale_visibility"] != "personal" else None, d["rationale_visibility"], d["plan_id"], d["recorded_at"]))] or [d["node_key"]]
        d["why_visibility"] = "restricted" if d.get("rationale_visibility") == "personal" else "shared"
        if d["why_visibility"] == "restricted":
            d["rationale"] = None
        out.append(d)
    return out


def record_constraint(conn, *, project: str, subjects, statement: str, rationale: str | None = None,
                      plan_id: str | None = None, why_ref: str | None = None, why_visibility: str = "shared",
                      state: str = "active", commit: bool = True) -> int | None:
    """DP phase 1 (Task 7): a constraint as change_reason rows — one per anchored
    subject (role constraint, recorded_by human, asserted), the why_ref as an
    unreachable doc reference linked to each. Returns the first row's id (the
    id anchored_constraints will report), None when there is no subject.
    ledger_store.add_entry calls this so a new ledger constraint is readable
    where the readers now look."""
    subjects = [s for s in (subjects or []) if s]
    if not subjects:
        return None
    refs = []
    if why_ref:
        refs.append(provenance.insert_reference(conn, project=project, kind="doc", label=str(why_ref)[:512],
                                                occurred_at=conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S','now')").fetchone()[0],
                                                commit=False))
    first = None
    for subject in subjects:
        rid = provenance.insert_reason(conn, project=project, plan_id=plan_id or "ledger", node_key=subject, kind="organizational",
                                       role="constraint", statement=statement, rationale=rationale or None,
                                       rationale_visibility="personal" if why_visibility == "restricted" else "shareable",
                                       refs=refs, recorded_by="human", state=state, commit=False)
        first = first if first is not None else rid
    if commit:
        conn.commit()
    return first


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
        ctx = json.loads(plan.get("impact_context") or "{}")
    except ValueError:
        ctx = {}
    seen = set(ctx.get("constraint_ids", []))
    # the ids impact_preflight stored may come from the ledger row while the
    # reader here returns change_reason ids: a constraint whose statement was
    # surfaced on any symbol at plan time counts as read either way
    seen_statements = {c.get("statement") for sym in (ctx.get("symbols") or []) for c in (sym.get("constraints") or []) if isinstance(c, dict)}
    already = {r[0] for r in conn.execute(
        "SELECT interpretation FROM change_reason WHERE plan_id = ? AND role = 'constraint' AND rule_id = 'constraint_bypassed'", (plan_id,))}
    already |= {r["text"] for r in db.get_node_reasons(conn, plan_id=plan_id) if r["kind"] == "constraint_ref"}
    run_id = max((c["run_id"] for c in changed), default=None)
    n = 0
    for c in hit:
        if c["id"] in seen or c.get("statement") in seen_statements:
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
