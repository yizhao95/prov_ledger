"""FL-006: the analyzer reaches the backend through ONE shim, analyzer/_host.py —
`provledger` (the installed package) first, the bundled `orchestrator` source
tree as the fallback. Nothing else under the skill splices orchestrator-backend
into sys.path."""
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parents[2]


def test_host_exposes_the_backend_modules():
    from analyzer import _host
    assert _host.SOURCE in ("provledger", "bundled")
    assert hasattr(_host.graph_api, "NodeObservation") and hasattr(_host.extensions, "load")
    assert _host.graph_api.__name__.endswith("graph_api")


def test_host_bundled_fallback_works_without_the_package():
    code = ("import sys; sys.modules['provledger'] = None; sys.path.insert(0, %r); "
            "from analyzer import _host; print(_host.SOURCE, _host.graph_api.__name__)") % str(SCRIPTS)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(SCRIPTS))
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["bundled", "orchestrator.graph_api"]


def test_only_host_mentions_the_bundled_backend_path():
    offenders = []
    for p in SCRIPTS.rglob("*.py"):
        if ".venv" in p.parts or p.name in ("_host.py", "test_host_import.py"):
            continue
        if "orchestrator-backend" in p.read_text(encoding="utf-8", errors="replace"):
            offenders.append(str(p.relative_to(SCRIPTS)))
    assert offenders == [], offenders
    # repo-wide: no single line splices orchestrator-backend into sys.path except the shim
    lines = []
    for p in REPO.rglob("*.py"):
        if ".venv" in p.parts or "node_modules" in p.parts or p.name in ("_host.py", "test_host_import.py"):
            continue
        for n, ln in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "sys.path.insert" in ln and "orchestrator-backend" in ln:
                lines.append(f"{p.relative_to(REPO)}:{n}")
    assert lines == [], lines


def test_analyzer_uv_env_can_import_provledger():
    """A third-party provider imports `provledger.graph_api`; the analyzer's own
    uv environment (init_project.sh runs `uv run python -m analyzer`) must
    therefore contain the package, and _host must pick it there (acceptance C)."""
    r = subprocess.run(["uv", "run", "--quiet", "python", "-c",
                        "import provledger.graph_api; from analyzer import _host; print(_host.SOURCE)"],
                       capture_output=True, text=True, cwd=str(SCRIPTS), timeout=300)
    assert r.returncode == 0, r.stderr[-800:]
    assert r.stdout.strip() == "provledger"
