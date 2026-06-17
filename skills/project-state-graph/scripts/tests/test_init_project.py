"""End-to-end test for init_project.sh — the one-command orchestrator.

Builds a tiny 2-file python repo in a temp dir, runs init_project.sh against it
with an ISOLATED registry root (PSG_REGISTRY_ROOT), and asserts that both layers
+ the registry + the index were produced.
"""
import json
import os
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
INIT_SH = SCRIPTS_DIR / "init_project.sh"


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text(
        "def index():\n    return helper()\n\n"
        "def helper():\n    return 1\n"
    )
    (repo / "util.py").write_text("def shared():\n    return 2\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=a@b.c",
         "-c", "user.name=t", "commit", "-qm", "init"],
        check=True,
    )
    return repo


def test_init_project_end_to_end(tmp_path):
    repo = _make_repo(tmp_path)
    out_dir = tmp_path / "out"
    registry_root = tmp_path / "registry"

    env = dict(os.environ)
    env["PSG_REGISTRY_ROOT"] = str(registry_root)

    result = subprocess.run(
        ["bash", str(INIT_SH),
         "--name", "demo", "--repo", str(repo), "--out-dir", str(out_dir)],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (
        f"init_project.sh failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )

    # Deep + shallow layers exist.
    db = out_dir / "demo-state-graph.db"
    arch = out_dir / "ARCHITECTURE.md"
    assert db.exists(), f"missing deep DB: {db}"
    assert arch.exists(), f"missing ARCHITECTURE.md: {arch}"

    arch_text = arch.read_text()
    assert "main.py" in arch_text
    assert "util.py" in arch_text

    # Registry recorded the project.
    registry_json = registry_root / "projects.json"
    assert registry_json.exists(), "registry not written"
    data = json.loads(registry_json.read_text())
    names = {p["name"] for p in data["projects"]}
    assert "demo" in names

    # Index mentions the project.
    index_md = registry_root / "PROJECT-STATE-GRAPHS.md"
    assert index_md.exists(), "index not written"
    assert "demo" in index_md.read_text()

    # Self-check passed (orchestrator runs it; confirm reflected in stdout).
    assert "Self-check: PASS" in result.stdout
