"""outcomes — close-time backfill of what a plan's expectations turned into
(spec §3.4/§3.5, path C). Runs when a registered-project plan closes reviewed:
every expectation of OTHER plans of the project that has no outcome of a
kind yet gets one — observed (a real channel), survival (derived from the
state graph) or none_available (honestly nothing to observe, with the reason).
Idempotent: get_pending_expectations excludes what already has an outcome.
Never blocks a close: any failure becomes a none_available with the error.
"""
from __future__ import annotations

import json

from . import db, extensions, outcome_channels, psg_bridge, survival


def _observed(conn, e: dict, closing_plan_id: str) -> tuple[str, dict, str, str, str | None]:
    """-> (kind, value, source, tier, reason) for the `observed` slot of one
    expectation. Phase 7: every applicable outcome channel (built-ins +
    extensions.outcome_channels, highest priority first) is asked in turn; the
    first non-None answer wins. A channel that raises is recorded under
    value['channel_errors'] and skipped. Nothing applicable / nothing said ->
    none_available naming the channels that were asked."""
    ch = e.get("channel") or "none"
    if ch == "none":
        return ("none_available", {"channel": ch}, "none", "none", e.get("claim") or "no observation channel declared")
    ext = extensions.current(psg_bridge.repo_for(e["project"]))
    asked: list[str] = []
    errors: dict[str, str] = {}
    for chan in outcome_channels.load_channels(ext):
        try:
            if not chan.applicable_to(e):
                continue
        except Exception as exc:  # noqa: BLE001
            errors[chan.channel_id] = f"{type(exc).__name__}: {exc}"
            continue
        asked.append(chan.channel_id)
        try:
            r = chan.collect(conn, e, closing_plan_id=closing_plan_id, extensions=ext)
        except Exception as exc:  # noqa: BLE001 — one broken channel never blocks a close
            errors[chan.channel_id] = f"{type(exc).__name__}: {exc}"
            continue
        if r is None:
            continue
        kind, value, source, tier, reason = r
        if errors:
            value = {**value, "channel_errors": errors}
        return (kind, value, source, tier, reason)
    available = [c.channel_id for c in outcome_channels.load_channels(ext)]
    detail = f"asked {asked}" if asked else f"no channel applies (available: {available})"
    if errors:
        detail += f"; errors {errors}"
    return ("none_available", {"channel": ch, "asked": asked, "channel_errors": errors}, ch.split(":")[0], "none",
            f"no observation for channel {ch!r}: {detail}")


def _verdict_key(sig: dict) -> tuple:
    """What makes two survival verdicts 'the same': the signal and the plans
    behind it (churned by three plans is news after churned by two)."""
    return (sig.get("signal"), tuple(sig.get("plans") or sig.get("changed_by") or ()))


def _latest_survival_key(conn, expectation_id: int) -> tuple | None:
    """The verdict key of the expectation's latest survival-path outcome (a
    survival row, or a none_available written by the survival path)."""
    rows = [o for o in db.get_outcomes(conn, expectation_id)
            if o["kind"] == "survival" or (o["kind"] == "none_available" and o["source"] == "state_graph")]
    if not rows:
        return None
    try:
        value = json.loads(rows[-1]["value_json"] or "{}")
    except ValueError:
        value = {}
    return _verdict_key(value) if rows[-1]["kind"] == "survival" else ("unknown", ())


def backfill(conn, project: str, psg_db_path: str | None, closing_plan_id: str) -> dict:
    """Produce the missing outcomes for `project`'s pending expectations.
    Returns counts per kind plus errors (recorded as none_available)."""
    counts = {"observed": 0, "survival": 0, "none_available": 0, "skipped_graph_channel": 0, "errors": 0}
    # survival: node/column expectations, RE-JUDGED at every close (phase 5):
    # a new outcome is appended only when the verdict (signal + the plans
    # behind it) differs from the expectation's latest survival outcome.
    for e in db.get_pending_expectations(conn, project, exclude_plan_id=closing_plan_id, kind="survival"):
        if e["target_kind"] not in ("node", "column") and e["channel"] != "graph":
            continue
        try:
            consumers = psg_bridge.output_consumers(psg_db_path, e["target"])
            sig = survival.derive(psg_db_path, e["target"], e["created_at"], consumers=consumers)
            if _verdict_key(sig) == _latest_survival_key(conn, e["id"]):
                continue                                   # same verdict as last time: nothing new to record
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
