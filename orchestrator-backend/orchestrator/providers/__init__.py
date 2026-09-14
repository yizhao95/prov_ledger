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
import warnings

from ..graph_api import KNOWN_LAYERS, TYPE_ID_RE, NodeObservation, validate_attrs

DEFAULT_TIMEOUT_S = 30.0
ISOLATE_MODES = ("thread", "subprocess")


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


def run_provider(provider, ctx, timeout_s: float = DEFAULT_TIMEOUT_S, isolate: str = "thread"):
    """-> (observations, degraded, elapsed_s). degraded is None on success,
    otherwise the reason the provider's output was discarded WHOLE.

    isolate="thread" (default): extract() runs in a daemon thread; on timeout
    the thread is abandoned (it keeps running until the process exits).
    isolate="subprocess": extract() runs in a forked child that is KILLED on
    timeout — a hard budget, at the cost of a fork per provider (Linux/macOS;
    platforms without fork degrade with a reason). Observations travel back
    pickled, so a provider's attrs must be picklable."""
    if isolate not in ISOLATE_MODES:
        raise ValueError(f"isolate must be one of {ISOLATE_MODES}, got {isolate!r}")
    if not TYPE_ID_RE.match(getattr(provider, "type_id", "") or ""):
        return [], f"type_id {getattr(provider, 'type_id', None)!r} is not namespaced vendor.name", 0.0
    if isolate == "subprocess":
        obs, degraded, elapsed = _extract_in_subprocess(provider, ctx, timeout_s)
    else:
        obs, degraded, elapsed = _extract_in_thread(provider, ctx, timeout_s)
    if degraded:
        return [], degraded, elapsed
    problems = check_observations(provider, obs)
    if problems:
        head = "; ".join(problems[:3]) + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else "")
        return [], f"schema: {len(problems)} violation(s) — {head}", elapsed
    return obs, None, elapsed


def _extract_in_thread(provider, ctx, timeout_s: float):
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
    return box.get("obs", []), None, elapsed


def _extract_in_subprocess(provider, ctx, timeout_s: float):
    """Fork, run extract() in the child, ship the observations back over a
    pipe, kill the child if the budget runs out. The child inherits ctx
    (including its read-only sqlite connection) — nothing is pickled on the
    way in, only the observations on the way out."""
    import multiprocessing as mp
    try:
        mpctx = mp.get_context("fork")
    except ValueError:
        return [], "subprocess isolation needs the fork start method (unavailable on this platform)", 0.0
    parent_end, child_end = mpctx.Pipe(duplex=False)

    def target(pipe):
        try:
            pipe.send(("ok", list(provider.extract(ctx))))
        except BaseException as e:  # noqa: BLE001
            try:
                pipe.send(("err", f"{type(e).__name__}: {e}"))
            except BaseException:  # noqa: BLE001
                pass
        finally:
            pipe.close()

    t0 = time.monotonic()
    proc = mpctx.Process(target=target, args=(child_end,), name=f"provider:{provider.type_id}", daemon=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)   # fork() in a threaded parent: intended here
        proc.start()
    child_end.close()
    msg = None
    try:
        if parent_end.poll(timeout_s):
            msg = parent_end.recv()
    except (EOFError, OSError):
        msg = None
    finally:
        parent_end.close()
    elapsed = time.monotonic() - t0
    if msg is None:
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        if elapsed >= timeout_s:
            return [], f"timeout after {timeout_s}s (budget exceeded; the subprocess was killed)", elapsed
        return [], f"subprocess exited without a result (exitcode {proc.exitcode})", elapsed
    proc.join(5)
    if proc.is_alive():
        proc.kill()
        proc.join(5)
    kind, payload = msg
    if kind == "err":
        return [], payload, elapsed
    return payload, None, elapsed


def builtin_providers() -> list:
    """The two reference providers, in the host's emission order."""
    from .builtin_owned import BuiltinOwnedProvider
    from .builtin_symbols import BuiltinSymbolProvider
    return [BuiltinSymbolProvider(), BuiltinOwnedProvider()]


def make_context(db_path: str, repo_root: str, run_id: int, file_map=None):
    """An ExtractionContext over a built graph: a read-only connection (usable
    from the provider thread) and node_rows() over this run's not-yet-stamped
    node rows -> (id, node_type, qualified_name, name, file_path, line_start, line_end, dtype)."""
    import sqlite3

    from ..graph_api import ExtractionContext
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)

    def node_rows(kinds: tuple[str, ...]) -> list:
        ph = ",".join("?" for _ in kinds) or "''"
        return conn.execute(
            f"""SELECT n.id, t.name, n.qualified_name, n.name, n.file_path, n.line_start, n.line_end, n.dtype
                FROM node n JOIN node_type t ON n.node_type_id=t.id
                WHERE t.name IN ({ph}) AND n.run_id IS NULL ORDER BY n.id""", tuple(kinds)).fetchall()

    return ExtractionContext(repo_root=repo_root, conn_ro=conn, run_id=run_id, file_map=dict(file_map or {}),
                             node_rows=node_rows)


HOST_CAPABILITIES: tuple[str, ...] = ()      # "runtime_capture" | "network" | "llm": none offered yet


def _record(pid: str, module, schema_version, enabled: bool, priority: int, degraded, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    return {"id": pid, "module": module, "schema_version": schema_version, "enabled": enabled,
            "priority": priority, "degraded": degraded, "timeout_s": timeout_s}


def load_providers(extensions, builtin: bool = True):
    """-> (providers, records). The built-ins (priority 0, disable-able by
    declaring their id with enabled=false) plus every provider declared in the
    extensions file, imported by `pkg.mod:Class`. Nothing here raises: an import
    error, a class that is not a NodeTypeProvider, a type_id that differs from
    the declared id, or a required capability the host does not offer is a
    degradation record and the provider is left out. Ordered by priority
    (larger first), then declaration order (built-ins first)."""
    import importlib

    from ..graph_api import NodeTypeProvider
    decls = {d.id: d for d in getattr(extensions, "providers", ())}
    loaded: list[tuple[int, int, object]] = []
    records: list[dict] = []
    order = 0
    if builtin:
        for p in builtin_providers():
            d = decls.pop(p.type_id, None)
            enabled = d.enabled if d else True
            prio = d.priority if d else 0
            records.append(_record(p.type_id, None, p.schema_version, enabled, prio, None, d.timeout_s if d else DEFAULT_TIMEOUT_S))
            if enabled:
                loaded.append((-prio, order, p))
            order += 1
    for d in decls.values():
        degraded = None
        provider = None
        if not d.enabled:
            records.append(_record(d.id, d.module, None, False, d.priority, None, d.timeout_s))
            continue
        try:
            mod_name, _, cls_name = (d.module or "").partition(":")
            mod = importlib.import_module(mod_name)
            cls = getattr(mod, cls_name)
            provider = cls()
        except Exception as e:  # noqa: BLE001 — every failure degrades
            degraded = f"import failed: {type(e).__name__}: {e}"
        if degraded is None and not isinstance(provider, NodeTypeProvider):
            degraded = f"{d.module} is not a NodeTypeProvider (type_id/schema_version/requires/extract/attributes_schema/declared_stability)"
        if degraded is None and getattr(provider, "type_id", None) != d.id:
            degraded = f"declared id {d.id!r} but the class says type_id {getattr(provider, 'type_id', None)!r}"
        if degraded is None:
            missing = [c for c in getattr(provider, "requires", ()) if c not in HOST_CAPABILITIES]
            if missing:
                degraded = f"capability {missing[0]!r} unavailable on this host (offers {list(HOST_CAPABILITIES)})"
        records.append(_record(d.id, d.module, getattr(provider, "schema_version", None), True, d.priority, degraded, d.timeout_s))
        if degraded is None:
            loaded.append((-d.priority, order, provider))
        order += 1
    loaded.sort(key=lambda t: (t[0], t[1]))
    return [p for _, _, p in loaded], records
