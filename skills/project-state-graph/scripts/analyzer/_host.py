"""The one place the project-state-graph analyzer reaches the backend (FL-006).

`provledger` (the installed package, editable in the unified venv) first; the
bundled `orchestrator` source tree as the fallback for a bare checkout.
Nothing else under this skill may splice orchestrator-backend into sys.path.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from provledger import extensions, graph_api, providers, testing  # type: ignore
    SOURCE = "provledger"
except ImportError:
    _BUNDLED = Path(__file__).resolve().parents[4] / "orchestrator-backend"
    if str(_BUNDLED) not in sys.path:
        sys.path.insert(0, str(_BUNDLED))
    from orchestrator import extensions, graph_api, providers, testing  # type: ignore
    SOURCE = "bundled"

__all__ = ["SOURCE", "extensions", "graph_api", "providers", "testing"]
