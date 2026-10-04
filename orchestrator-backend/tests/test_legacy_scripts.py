"""The two legacy entry points that live outside the package run from a clean
checkout with the unified venv: `orchestrator-cli.py` at the repo root and
`orchestrator-backend/backfill_from_markdown.py`.

Both import `orchestrator`, which the venv does not provide (it installs the
package as `provledger`), so each script must put `orchestrator-backend/` —
the directory that CONTAINS the package — on sys.path itself. The root CLI used
to add `<repo>/orchestrator`, a directory that does not exist, and failed with
ModuleNotFoundError before it could print its own help.

Each runs in a subprocess from an unrelated cwd, with no PYTHONPATH, and with
ORCH_DB pointed into tmp so nothing can reach the real ledger."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = {
    "orchestrator-cli.py": (REPO / "orchestrator-cli.py", "init-plan"),
    "backfill_from_markdown.py": (REPO / "orchestrator-backend" / "backfill_from_markdown.py", "--dry-run"),
}


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_the_legacy_script_prints_its_help_from_a_clean_checkout(tmp_path, name):
    script, expected = SCRIPTS[name]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(ORCH_DB=str(tmp_path / "orch.db"), PSG_REGISTRY_PATH=str(tmp_path / "projects.json"),
               PSG_REGISTRY_ROOT=str(tmp_path / "graphs"))
    r = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True,
                       cwd=str(tmp_path), env=env, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    assert expected in r.stdout
    assert not (tmp_path / "orch.db").exists(), "--help must not open a database"
