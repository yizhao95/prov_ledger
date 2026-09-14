"""Outcome channels (phase 7): HOW an expectation's `observed` outcome is
collected. Each channel answers for the expectation channels it recognises
(`profile_drift`, `metric:<name>`, a vendor id) and returns the same shape
outcomes._observed always returned — (kind, value, source, tier, reason) —
or None when it has nothing to say, so the next channel is asked.

Two built-ins: ProfileDriftChannel (the data_profile before/after diff) and
MetricChannel (the nearest metrics row before and after the expectation).
Third-party channels are declared in provledger-extensions.json under
`outcome_channels` (module:Class); a channel that cannot be imported or
raises inside collect() is isolated — recorded, never fatal to a close.
Values are MEASURED by the channel from stored rows; no channel is ever
handed to a model.
"""
from __future__ import annotations

import importlib
from typing import Protocol, runtime_checkable

from . import db, drift, extensions as _ext

Result = tuple[str, dict, str, str, str | None]      # (kind, value, source, tier, reason)


@runtime_checkable
class OutcomeChannel(Protocol):
    channel_id: str                                   # 'profile_drift' | 'metric' | 'vendor.name'

    def applicable_to(self, expectation: dict) -> bool: ...

    def collect(self, conn, expectation: dict, *, closing_plan_id: str, extensions) -> Result | None: ...


class ProfileDriftChannel:
    """expectation.channel == 'profile_drift': diff the latest data_profile
    snapshot before the expectation against the latest one after it."""
    channel_id = "profile_drift"

    def applicable_to(self, expectation: dict) -> bool:
        return (expectation.get("channel") or "") == "profile_drift"

    def collect(self, conn, e: dict, *, closing_plan_id: str, extensions) -> Result | None:
        before, after = _profile_rows(conn, e["target"], e["project"], e["created_at"])
        if not before or not after:
            return ("none_available", {"before_rows": len(before), "after_rows": len(after)}, "data_profile", "none",
                    "no data_profile snapshot before and after the expectation")
        drifts = drift.detect_drift(before, after, extensions=extensions)
        return ("observed", {"drifts": drifts, "kinds": sorted({d["kind"] for d in drifts})}, "data_profile", "observed", None)


class MetricChannel:
    """expectation.channel == 'metric:<name>': the nearest metrics row at or
    before the expectation and the nearest one after it -> before / after /
    delta / delta_pct. A missing side is none_available and says which."""
    channel_id = "metric"

    def applicable_to(self, expectation: dict) -> bool:
        ch = expectation.get("channel") or ""
        return ch.startswith("metric:") and len(ch) > len("metric:")

    def collect(self, conn, e: dict, *, closing_plan_id: str, extensions) -> Result | None:
        name = e["channel"][len("metric:"):]
        created = e["created_at"]
        befores = db.get_metrics(conn, e["project"], name, before=created)
        afters = db.get_metrics(conn, e["project"], name, after=created)
        if not befores or not afters:
            missing = "before" if not befores else "after"
            return ("none_available", {"name": name, "before_rows": len(befores), "after_rows": len(afters)},
                    "metrics", "none",
                    f"no metric {name!r} observed {missing} the expectation")
        b, a = befores[-1], afters[0]
        delta = a["value"] - b["value"]
        pct = round(100.0 * delta / abs(b["value"]), 4) if b["value"] else None
        return ("observed", {"name": name, "before": b["value"], "after": a["value"], "delta": round(delta, 6),
                             "delta_pct": pct, "unit": a.get("unit") or b.get("unit"),
                             "before_at": b["observed_at"], "after_at": a["observed_at"],
                             "before_plan": b.get("plan_id"), "after_plan": a.get("plan_id")},
                "metrics", "observed", None)


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


BUILTIN_CHANNELS = (ProfileDriftChannel, MetricChannel)


def _import_channel(decl) -> OutcomeChannel:
    mod_name, _, cls_name = decl.module.partition(":")
    cls = getattr(importlib.import_module(mod_name), cls_name)
    inst = cls()
    if not isinstance(inst, OutcomeChannel):
        raise TypeError(f"{decl.module} is not an OutcomeChannel (needs channel_id, applicable_to, collect)")
    if getattr(inst, "channel_id", None) != decl.id:
        raise ValueError(f"declared id {decl.id!r} != class channel_id {getattr(inst, 'channel_id', None)!r}")
    return inst


def load_channels(extensions, report: dict | None = None) -> list[OutcomeChannel]:
    """The channels to ask, highest priority first (ties: built-ins, then
    declaration order). Built-ins can be disabled by declaring their id with
    enabled=false. `report` (optional dict) receives one record per declared
    channel: {module, enabled, priority, degraded}."""
    decls = list(getattr(extensions, "outcome_channels", ()) or ())
    disabled = {d.id for d in decls if not d.enabled}
    ordered: list[tuple[int, int, OutcomeChannel]] = []
    n = 0
    for cls in BUILTIN_CHANNELS:
        inst = cls()
        if inst.channel_id not in disabled:
            ordered.append((0, n, inst)); n += 1
    for d in decls:
        rec = {"module": d.module, "enabled": d.enabled, "priority": d.priority, "degraded": None}
        if report is not None:
            report[d.id] = rec
        if not d.enabled or d.module is None:
            continue
        try:
            inst = _import_channel(d)
        except Exception as exc:  # noqa: BLE001 — isolation is the point
            rec["degraded"] = f"{type(exc).__name__}: {exc}"
            continue
        ordered.append((d.priority, n, inst)); n += 1
    ordered.sort(key=lambda t: (-t[0], t[1]))
    return [c for _, _, c in ordered]
