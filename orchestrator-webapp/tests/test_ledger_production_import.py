"""The dashboard must run under the name it is INSTALLED as (FL-067, webapp side).

In production the package is `provledger` — an editable/wheel install in the
venv. `orchestrator` is a repo-internal name that exists only because pytest
puts `orchestrator-backend` on `sys.path`. So a route doing
`from orchestrator import …` passes every test in this suite and returns 500
from the running server, which is the worst shape a bug can have: the tests say
the opposite of the truth.

Two guards, because either one alone can be fooled:

  · a **static** check that no webapp source names the repo-internal package;
  · a **subprocess** that imports and serves the app on a path where that name
    genuinely does not resolve, and asks `/ledger` a real question.

The subprocess cannot just use the venv's own `provledger`: that editable
install points at the main checkout, which does not carry this branch. So it
maps the name onto THIS tree (a directory holding `provledger -> <worktree>/
orchestrator-backend/orchestrator`) and disables the editable finder, which
otherwise wins over `sys.path`. What is being simulated is the NAME, not
someone else's checkout.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WEBAPP = REPO / "orchestrator-webapp"
# the one path this file legitimately knows, because building a production-shaped
# import path is its subject; conftest.py does the sys.path wiring (FL-006)
BACKEND = REPO / "orchestrator-backend"

from test_routes import _seed_db, _seed_reasons_and_constraints, _seed_state_graph  # noqa: E402

FORBIDDEN = re.compile(r"^\s*(?:from\s+orchestrator[\s.]|import\s+orchestrator\b)", re.M)


def test_no_webapp_source_names_the_repo_internal_package():
    """`orchestrator` is not installed anywhere a user runs this from."""
    offenders = []
    for path in sorted((WEBAPP / "app").rglob("*.py")):
        if FORBIDDEN.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO)))
    for path in sorted((WEBAPP / "app" / "templates").rglob("*.html")):
        if FORBIDDEN.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], (
        "webapp source imports the repo-internal name `orchestrator`; production only has "
        f"`provledger`, so these are 500s the suite cannot see: {offenders}")


PROBE = r'''
import json, os, sys

shim, webapp, db, registry = sys.argv[1:5]

# the editable install's MetaPathFinder wins over sys.path, and it points at the
# main checkout; drop it so `provledger` resolves to the tree under test
sys.meta_path = [f for f in sys.meta_path
                 if "__editable__" not in getattr(getattr(f, "__class__", None), "__module__", "")]
for name in [m for m in sys.modules if m == "provledger" or m.startswith("provledger.")]:
    del sys.modules[name]
sys.path = [p for p in sys.path if "orchestrator-backend" not in p]
sys.path.insert(0, shim)
sys.path.insert(0, webapp)

try:
    import orchestrator                                        # noqa: F401
except ImportError:
    pass
else:
    print(json.dumps({"error": "the repo-internal name still resolves; this is not a production-shaped path"}))
    raise SystemExit(1)

import provledger, pathlib
resolved = str(pathlib.Path(provledger.__file__).resolve())
assert "orchestrator-backend" in resolved, resolved       # the tree under test, reached by its installed name

os.environ["ORCH_DB"] = db
os.environ["PSG_REGISTRY_PATH"] = registry

from fastapi.testclient import TestClient
from app import main

c = TestClient(main.app)
r = c.get("/ledger?q=why+must+load_orders+keep+paid+orders+only&project=demo")
print(json.dumps({"status": r.status_code,
                  "unavailable": main.ASK_UNAVAILABLE in r.text,
                  "scope": "Scope:" in r.text,
                  "facts": "Fact table" in r.text,
                  "absence": 'data-absence=' in r.text}))
'''


@pytest.fixture
def production_path(tmp_path):
    """A directory where the backend is importable ONLY as `provledger`."""
    shim = tmp_path / "site-packages"
    shim.mkdir()
    (shim / "provledger").symlink_to(BACKEND / "orchestrator", target_is_directory=True)
    return shim


def test_the_ledger_page_answers_under_the_installed_package_name(tmp_path, production_path):
    dbp = tmp_path / "orch.db"
    _seed_db(dbp)
    _, registry = _seed_state_graph(tmp_path)
    _seed_reasons_and_constraints(dbp)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ORCH_DB", "PSG_REGISTRY_PATH")}
    p = subprocess.run([sys.executable, "-c", PROBE, str(production_path), str(WEBAPP), str(dbp), str(registry)],
                       capture_output=True, text=True, cwd=str(tmp_path), env=env, timeout=180)
    assert p.returncode == 0, f"the probe failed:\n{p.stdout}\n{p.stderr[-3000:]}"
    got = json.loads(p.stdout.strip().splitlines()[-1])
    assert got["status"] == 200, got
    assert got["unavailable"] is False, "the page fell back to 'ask unavailable' — the import did not work"
    assert got["scope"] and got["facts"] and got["absence"], got


def test_the_page_says_so_instead_of_500_when_the_package_is_missing(tmp_path, monkeypatch):
    """The guard's other half: if `provledger` really is absent, the page must
    still be a page — an operator reads a sentence, not a stack trace."""
    import importlib
    from app import main
    monkeypatch.setattr(main, "ask_mod", None)
    from fastapi.testclient import TestClient
    dbp = tmp_path / "orch.db"
    _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    c = TestClient(main.app)
    r = c.get("/ledger?q=anything&project=demo")
    assert r.status_code == 200 and main.ASK_UNAVAILABLE in r.text
    assert c.get("/ledger/card?ask_id=1").status_code == 503
    importlib.reload(main)
