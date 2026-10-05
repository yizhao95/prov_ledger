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


def test_init_project_keeps_history_across_refresh(tmp_path):
    """Phase-1 dogfood finding: init_project.sh used to rm the DB before every
    rebuild, wiping node_snapshot/node_event. A refresh must keep them."""
    import sqlite3
    repo = _make_repo(tmp_path)
    out_dir = tmp_path / "out"
    env = dict(os.environ, PSG_REGISTRY_ROOT=str(tmp_path / "registry"),
               PROVLEDGER_PLAN_ID="plan-x", PROVLEDGER_STEP_ID="plan-x-REVIEW.1", PROVLEDGER_TRIGGER="review")
    for _ in range(2):
        result = subprocess.run(["bash", str(INIT_SH), "--name", "demo", "--repo", str(repo), "--out-dir", str(out_dir)],
                                capture_output=True, text=True, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
    conn = sqlite3.connect(str(out_dir / "demo-state-graph.db"))
    runs = [r for r in conn.execute("SELECT id, plan_id, step_id, trigger FROM analysis_run ORDER BY id")]
    assert len(runs) == 2 and runs[1][1:] == ("plan-x", "plan-x-REVIEW.1", "review")
    assert {r[0] for r in conn.execute("SELECT DISTINCT run_id FROM node_snapshot")} == {runs[0][0], runs[1][0]}
    assert conn.execute("SELECT COUNT(*) FROM node_event WHERE event_type='node_matched' AND run_id=?",
                        (runs[1][0],)).fetchone()[0] > 0
    conn.close()
    # the cold archive of the first build is still taken, named by the real sha
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert (out_dir / f"provledger.{sha}.db").exists(), sorted(p.name for p in out_dir.iterdir())


def test_registry_path_alone_keeps_every_write_beside_it(tmp_path):
    """PSG_REGISTRY_PATH is the variable the demo, the examples suite and the plan
    scripts set to isolate a run. With only it set, init_project.sh used to derive
    the index (and the default --out-dir) from PSG_REGISTRY_ROOT's default — the
    real ~/skill-workspace/project-graphs — so regenerate_index overwrote the
    developer's PROJECT-STATE-GRAPHS.md. HOME is a temp dir here, so a regression
    shows up as a file under it instead of damage to the real one."""
    repo = _make_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    reg_dir = tmp_path / "reg"
    env = dict(os.environ, HOME=str(home), PSG_REGISTRY_PATH=str(reg_dir / "projects.json"),
               ORCH_DB=str(tmp_path / "orchestrator.db"),
               PROVLEDGER_HOOK_ERRORS=str(tmp_path / "hook-errors.log"))
    for k in ("PSG_REGISTRY_ROOT", "PSG_INDEX_PATH",
              "PROVLEDGER_PLAN_ID", "PROVLEDGER_STEP_ID", "PROVLEDGER_TRIGGER"):
        env.pop(k, None)
    result = subprocess.run(["bash", str(INIT_SH), "--name", "demo", "--repo", str(repo)],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout + result.stderr

    leaked = home / "skill-workspace"
    assert not leaked.exists(), sorted(str(p.relative_to(home)) for p in leaked.rglob("*"))
    assert "demo" in {p["name"] for p in json.loads((reg_dir / "projects.json").read_text())["projects"]}
    assert "demo" in (reg_dir / "PROJECT-STATE-GRAPHS.md").read_text()
    assert (reg_dir / "demo" / "demo-state-graph.db").exists()


def test_registry_root_still_wins_for_the_index_when_both_are_set(tmp_path):
    """PSG_REGISTRY_ROOT keeps its meaning when it is given: the index and the
    default out-dir live under it, wherever the registry file is."""
    repo = _make_repo(tmp_path)
    root = tmp_path / "root"
    env = dict(os.environ, PSG_REGISTRY_ROOT=str(root),
               PSG_REGISTRY_PATH=str(tmp_path / "elsewhere" / "projects.json"),
               ORCH_DB=str(tmp_path / "orchestrator.db"))
    for k in ("PSG_INDEX_PATH", "PROVLEDGER_PLAN_ID", "PROVLEDGER_STEP_ID", "PROVLEDGER_TRIGGER"):
        env.pop(k, None)
    result = subprocess.run(["bash", str(INIT_SH), "--name", "demo", "--repo", str(repo)],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "elsewhere" / "projects.json").exists()
    assert (root / "PROJECT-STATE-GRAPHS.md").exists()
    assert (root / "demo" / "demo-state-graph.db").exists()
