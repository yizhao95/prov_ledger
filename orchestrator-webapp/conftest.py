"""Make the webapp suite import what the webapp will import in production.

Two long-standing traps, both fixed here rather than in each test file:

1. **FL-077** — `app` was importable only because one test file happened to
   insert its parent on `sys.path` during collection, so a new test file sorting
   before it broke the whole suite with an ImportError.

2. **FL-067, webapp side** — the backend is installed as `provledger`; the name
   `orchestrator` exists only on a developer's pythonpath. The venv may well
   carry a `provledger` editable install pointing at **another checkout** (this
   is how the phase-2e 500 hid behind 237 green tests: the routes imported
   `orchestrator`, which pytest provides and a server does not). So the suite
   binds `provledger` to **this tree**, through a real path entry, the way an
   install does — the editable finder is dropped because a MetaPathFinder wins
   over `sys.path` and would silently serve the other checkout.

The result is that a webapp test exercises this repository's backend under the
name production uses, and `from orchestrator import …` in `app/` is a failure
the suite can see (tests/test_ledger_production_import.py asserts it directly).
"""
from __future__ import annotations

import atexit
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "orchestrator-backend"
WEBAPP = REPO / "orchestrator-webapp"

for p in (str(BACKEND), str(WEBAPP)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _bind_installed_name() -> None:
    """`import provledger` must reach BACKEND, not whatever the venv points at."""
    spec = importlib.util.find_spec("provledger")
    origin = Path(spec.origin).resolve() if (spec and spec.origin) else None
    if origin is not None and BACKEND.resolve() in origin.parents:
        return                                            # already this tree
    sys.meta_path = [f for f in sys.meta_path
                     if "__editable__" not in getattr(getattr(f, "__class__", None), "__module__", "")]
    for name in [m for m in list(sys.modules) if m == "provledger" or m.startswith("provledger.")]:
        del sys.modules[name]
    shim = Path(tempfile.mkdtemp(prefix="provledger-shim-"))
    (shim / "provledger").symlink_to(BACKEND / "orchestrator", target_is_directory=True)
    sys.path.insert(0, str(shim))
    atexit.register(shutil.rmtree, shim, True)


_bind_installed_name()
