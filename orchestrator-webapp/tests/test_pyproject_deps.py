"""The webapp's own pyproject must be enough to run this suite.

FastAPI's TestClient needs `httpx`; it used to be listed only in the root
requirements.txt, so an environment built from this pyproject could not even
import the test client.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _names(specs: list[str]) -> set[str]:
    return {re.split(r"[\s<>=!~\[;]", s, maxsplit=1)[0].lower() for s in specs}


def test_the_test_dependencies_are_declared_where_test_deps_belong():
    data = tomllib.loads(PYPROJECT.read_text())
    dev = _names(data.get("dependency-groups", {}).get("dev", []))
    assert {"pytest", "httpx"} <= dev, f"dependency-groups.dev is {sorted(dev)}"


def test_the_runtime_dependencies_stay_runtime_only():
    data = tomllib.loads(PYPROJECT.read_text())
    runtime = _names(data["project"]["dependencies"])
    assert {"fastapi", "uvicorn", "jinja2"} <= runtime
    assert not {"pytest", "httpx"} & runtime, "test-only packages leaked into the runtime dependencies"
