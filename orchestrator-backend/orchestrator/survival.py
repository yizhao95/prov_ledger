"""survival — the free, weak signal about what happened to a node after a plan
claimed something about it (spec §3.3). Read-only over the project's state
graph (psg_bridge); pure derivation, tier `derived`.

  removed             a node_removed after the expectation
  reverted            a later change put struct_sig back to its value at the
                      time of the expectation
  churned             node_changed by >= 2 different plans since
  untouched_consumed  no change since and the node has output consumers
  untouched           no change since and no consumers

Survival is NOT correctness: an untouched node may be untouched because nobody
looked. Dashboards and docs must say so ("存活 ≠ 正确，弱证据").
"""
from __future__ import annotations

from . import psg_bridge

SIGNALS = ("removed", "reverted", "churned", "untouched_consumed", "untouched", "unknown")
CAVEAT = "survival is a weak signal: it says the node was not removed or reverted, not that it is correct"


def derive(psg_db_path: str | None, qualified_name: str, since_iso: str, *,
           consumers: list[str] | None = None) -> dict:
    """{signal, evidence: [event ids], events: n, caveat} — or
    {signal: 'unknown', reason} when the graph or the node is not there."""
    if not psg_db_path or psg_bridge.latest_run_id(psg_db_path) is None:
        return {"signal": "unknown", "reason": "state graph unavailable", "evidence": [], "events": 0}
    key = psg_bridge.node_key_of(psg_db_path, qualified_name)
    if key is None:
        return {"signal": "unknown", "reason": f"node {qualified_name!r} not in the state graph", "evidence": [], "events": 0}
    events = [e for e in psg_bridge.events_of(psg_db_path, key) if (e["created_at"] or "") > (since_iso or "")]
    changes = [e for e in events if e["event_type"] == "node_changed"]
    removed = [e for e in events if e["event_type"] == "node_removed"]
    out = {"node_key": key, "events": len(events), "caveat": CAVEAT}
    if removed:
        return {**out, "signal": "removed", "evidence": [e["event_id"] for e in removed]}
    if changes:
        at_expectation = (changes[0]["payload"].get("struct_sig") or {}).get("from")
        for e in changes[1:]:
            if at_expectation and (e["payload"].get("struct_sig") or {}).get("to") == at_expectation:
                return {**out, "signal": "reverted", "evidence": [changes[0]["event_id"], e["event_id"]]}
        plans = {e["plan_id"] for e in changes}
        if len(plans) >= 2:
            return {**out, "signal": "churned", "evidence": [e["event_id"] for e in changes], "plans": sorted(p or "" for p in plans)}
        # a single plan changed it: not reverted, not churned — it lives on, changed
        return {**out, "signal": "untouched_consumed" if consumers else "untouched",
                "evidence": [e["event_id"] for e in changes], "changed_by": sorted(p or "" for p in plans)}
    if consumers:
        return {**out, "signal": "untouched_consumed", "evidence": [], "consumers": sorted(consumers)}
    return {**out, "signal": "untouched", "evidence": []}
