"""Node-type providers (phase 6): the built-in reference implementations and
the host-side runner. `run_provider` is the ONLY way the host calls a
provider's extract(): it isolates failures (exception -> degraded, empty),
enforces the time budget (timeout -> degraded, empty) and rejects a whole
result set that breaks the provider's own schema or claims another type_id —
partial results never reach the graph.
"""
from __future__ import annotations

import threading
import time

from ..graph_api import KNOWN_LAYERS, TYPE_ID_RE, NodeObservation, validate_attrs

DEFAULT_TIMEOUT_S = 30.0


def check_observations(provider, observations) -> list[str]:
    """Every problem that makes a provider's output unusable."""
    problems: list[str] = []
    try:
        schema = provider.attributes_schema()
    except Exception as e:  # noqa: BLE001
        return [f"attributes_schema() raised {type(e).__name__}: {e}"]
    for i, o in enumerate(observations):
        if not isinstance(o, NodeObservation):
            problems.append(f"observations[{i}] is {type(o).__name__}, not a NodeObservation")
            continue
        where = f"observations[{i}] {o.qualified_name}"
        if o.type_id != provider.type_id:
            problems.append(f"{where}: type_id {o.type_id!r} != provider type_id {provider.type_id!r}")
        if not o.node_type or not o.qualified_name:
            problems.append(f"{where}: node_type and qualified_name are required")
        for s in o.signatures:
            if s.layer not in KNOWN_LAYERS and not s.layer.startswith("x-"):
                problems.append(f"{where}: unknown signature layer {s.layer!r} (known {list(KNOWN_LAYERS)} or x-…)")
        for e in validate_attrs(schema, o.attrs):
            problems.append(f"{where}: {e['detail']}")
    return problems


def run_provider(provider, ctx, timeout_s: float = DEFAULT_TIMEOUT_S):
    """-> (observations, degraded, elapsed_s). degraded is None on success,
    otherwise the reason the provider's output was discarded WHOLE."""
    if not TYPE_ID_RE.match(getattr(provider, "type_id", "") or ""):
        return [], f"type_id {getattr(provider, 'type_id', None)!r} is not namespaced vendor.name", 0.0
    box: dict = {}

    def target():
        try:
            box["obs"] = list(provider.extract(ctx))
        except BaseException as e:  # noqa: BLE001 — isolation is the point
            box["err"] = e

    t0 = time.monotonic()
    th = threading.Thread(target=target, name=f"provider:{provider.type_id}", daemon=True)
    th.start()
    th.join(timeout_s)
    elapsed = time.monotonic() - t0
    if th.is_alive():
        return [], f"timeout after {timeout_s}s (budget exceeded; the thread was abandoned)", elapsed
    if "err" in box:
        e = box["err"]
        return [], f"{type(e).__name__}: {e}", elapsed
    obs = box.get("obs", [])
    problems = check_observations(provider, obs)
    if problems:
        head = "; ".join(problems[:3]) + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else "")
        return [], f"schema: {len(problems)} violation(s) — {head}", elapsed
    return obs, None, elapsed
