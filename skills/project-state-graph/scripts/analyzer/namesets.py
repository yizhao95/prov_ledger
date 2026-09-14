"""Analyzer name sets (phase 5 Task 2) — the ONE source of the names the
analyzers recognise (split functions, fit/eval methods, hyper-parameter
variables, validators, DataFrame constructors, HTTP verbs/libraries).

DEFAULTS are the former module constants verbatim. A repo's
`provledger-extensions.json` (same file, same discovery rules as the backend's
orchestrator.extensions) patches them with `namesets` add/remove entries:
`cli.run` calls configure(repo_root) BEFORE walking; the analyzers call
get(name) inside their functions — never at import time, so a configure that
happens after import still applies. Entries of the same set apply in ascending
priority, so the highest priority lands last and wins.
"""
from __future__ import annotations

import sys
from pathlib import Path

DEFAULTS: dict[str, frozenset[str]] = {
    "split_funcs": frozenset({"train_test_split"}),
    "fit_methods": frozenset({"fit", "train"}),
    "eval_methods": frozenset({"predict", "score", "evaluate"}),
    "hp_names": frozenset({"param_grid", "params", "config", "hyperparams", "hparams", "parameters", "search_space"}),
    "validator_names": frozenset({"validate", "check", "check_schema", "expect", "assert_schema"}),
    "ml_call_names": frozenset({"train_test_split", "fit", "train"}),
    "df_constructors": frozenset({"DataFrame", "createDataFrame"}),
    "http_read_methods": frozenset({"get", "head", "options"}),
    "http_write_methods": frozenset({"post", "put", "patch", "delete"}),
    "http_libs": frozenset({"requests", "httpx", "aiohttp", "session", "client"}),
}

# The loader lives in the backend package (one file format, one validator).
_BUNDLED_ORCH = Path(__file__).resolve().parents[4] / "orchestrator-backend"
_DEV_ORCH = Path.home() / "skill-workspace" / "orchestrator"
for _root in (_BUNDLED_ORCH, _DEV_ORCH):
    if (_root / "orchestrator" / "extensions.py").exists() and str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
try:
    from orchestrator import extensions as _ext
    ExtensionsError = _ext.ExtensionsError
except ImportError:  # standalone checkout without the backend: defaults only
    _ext = None

    class ExtensionsError(ValueError):
        """The extensions file is malformed or ambiguous; nothing is applied."""

_ACTIVE: dict[str, frozenset[str]] | None = None


def configure(repo_root: str | None) -> dict | None:
    """Apply the discovered extensions file's namesets. Returns the fingerprint
    fragment {path, sha256, namesets} or None when no file applies. Any error
    leaves the previous configuration untouched."""
    global _ACTIVE
    if _ext is None:
        if repo_root and (Path(repo_root) / "provledger-extensions.json").exists():
            raise ExtensionsError("provledger-extensions.json found but the orchestrator package (its loader) is not importable")
        _ACTIVE = None
        return None
    ext = _ext.current(repo_root)
    if ext.path is None:
        _ACTIVE = None
        return None
    sets = {k: set(v) for k, v in DEFAULTS.items()}
    for ns in sorted(ext.namesets, key=lambda n: (n.priority, n.set)):
        if ns.set not in sets:
            raise ExtensionsError(f"namesets: unknown set {ns.set!r}; known sets: {sorted(DEFAULTS)}")
        sets[ns.set] |= set(ns.add)
        sets[ns.set] -= set(ns.remove)
    _ACTIVE = {k: frozenset(v) for k, v in sets.items()}
    return {"path": ext.path, "sha256": ext.sha256, "namesets": ext.fingerprint()["namesets"]}


def get(name: str) -> frozenset[str]:
    """The active set (DEFAULTS until configure applied a file). KeyError for an unknown set."""
    src = _ACTIVE if _ACTIVE is not None else DEFAULTS
    if name not in src:
        raise KeyError(f"unknown name set {name!r}; known: {sorted(DEFAULTS)}")
    return src[name]


def current() -> dict[str, frozenset[str]]:
    return dict(_ACTIVE if _ACTIVE is not None else DEFAULTS)


def reset() -> None:
    """Back to DEFAULTS (tests)."""
    global _ACTIVE
    _ACTIVE = None
