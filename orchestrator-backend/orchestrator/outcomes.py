"""outcomes — close-time backfill of what a plan's expectations turned into
(spec §3.4/§3.5, path C). Runs when a registered-project plan closes reviewed:
every expectation of OTHER plans of the project that has no outcome of a
kind yet gets one — observed (a real channel), survival (derived from the
state graph) or none_available (honestly nothing to observe, with the reason).
Idempotent: get_pending_expectations excludes what already has an outcome.
Never blocks a close: any failure becomes a none_available with the error.
"""
from __future__ import annotations

from . import db, drift, psg_bridge, survival


def _profile_rows(conn, dataset: str, project: str, created_at: str) -> tuple[list[dict], list[dict]]:
    """(latest snapshot before the expectation, latest snapshot after) as
    profile-row lists — one row per column, newest observation wins."""
    rows = db.get_data_profile(conn, dataset, project) or db.get_data_profile(conn, dataset)
    before: dict[str, dict] = {}
    after: dict[str, dict] = {}
    for r in rows:
        ts = r.get("observed_at") or r.get("created_at") or ""
        bucket = after if ts > created_at else before
        bucket[r["column_name"]] = r
    return list(before.values()), list(after.values())


def _observed(conn, e: dict, closing_plan_id: str) -> tuple[str, dict, str, str, str | None]:
    """-> (kind, value, source, tier, reason) for the `observed` slot of one expectation."""
    ch = e["channel"] or "none"
    if ch == "profile_drift":
        before, after = _profile_rows(conn, e["target"], e["project"], e["created_at"])
        if not before or not after:
            return ("none_available", {"before_rows": len(before), "after_rows": len(after)}, "data_profile", "none",
                    "no data_profile snapshot before and after the expectation")
        drifts = drift.detect_drift(before, after)
        return ("observed", {"drifts": drifts, "kinds": sorted({d["kind"] for d in drifts})}, "data_profile", "observed", None)
    if ch.startswith("metric:"):
        return ("none_available", {"channel": ch}, "metric", "none", f"no metric channel registered for {ch[7:]!r}")
    if ch == "none":
        return ("none_available", {"channel": ch}, "none", "none", e.get("claim") or "no observation channel declared")
    return ("none_available", {"channel": ch}, ch, "none", f"unknown channel {ch!r}")


def backfill(conn, project: str, psg_db_path: str | None, closing_plan_id: str) -> dict:
    """Produce the missing outcomes for `project`'s pending expectations.
    Returns counts per kind plus errors (recorded as none_available)."""
    counts = {"observed": 0, "survival": 0, "none_available": 0, "skipped_graph_channel": 0, "errors": 0}
    # survival: node expectations (target_kind node, or channel graph)
    for e in db.get_pending_expectations(conn, project, exclude_plan_id=closing_plan_id, kind="survival"):
        if e["target_kind"] != "node" and e["channel"] != "graph":
            continue
        try:
            consumers = psg_bridge.output_consumers(psg_db_path, e["target"])
            sig = survival.derive(psg_db_path, e["target"], e["created_at"], consumers=consumers)
            if sig["signal"] == "unknown":
                db.insert_outcome(conn, expectation_id=e["id"], kind="none_available", value=sig, source="state_graph",
                                  tier="none", reason=sig.get("reason"), backfilled_by_plan=closing_plan_id)
                counts["none_available"] += 1
            else:
                db.insert_outcome(conn, expectation_id=e["id"], kind="survival", value=sig, source="state_graph",
                                  tier="derived", reason=survival.CAVEAT, backfilled_by_plan=closing_plan_id)
                counts["survival"] += 1
        except Exception as exc:  # never block the close
            db.insert_outcome(conn, expectation_id=e["id"], kind="none_available", value={"error": str(exc)},
                              source="state_graph", tier="none", reason=f"survival derivation failed: {exc}",
                              backfilled_by_plan=closing_plan_id)
            counts["errors"] += 1
    # observed: every other channel
    for e in db.get_pending_expectations(conn, project, exclude_plan_id=closing_plan_id, kind="observed"):
        if e["channel"] == "graph":
            counts["skipped_graph_channel"] += 1      # survival already covers it; never written twice
            continue
        try:
            kind, value, source, tier, reason = _observed(conn, e, closing_plan_id)
            db.insert_outcome(conn, expectation_id=e["id"], kind=kind, value=value, source=source, tier=tier,
                              reason=reason, backfilled_by_plan=closing_plan_id)
            counts[kind] += 1
        except Exception as exc:
            db.insert_outcome(conn, expectation_id=e["id"], kind="none_available", value={"error": str(exc)},
                              source=e.get("channel") or "none", tier="none", reason=f"observation failed: {exc}",
                              backfilled_by_plan=closing_plan_id)
            counts["errors"] += 1
    return counts
