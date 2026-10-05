"""Tests for scripts/bootstrap.sh — the plugin's dependency self-setup.

Offline and fast: a fake `uv` shim is put first on PATH and logs its calls,
so we assert the script's *decisions* (create vs reuse vs no-op) without
touching the network or a real venv.

Regression anchor: uv >= 0.5 refuses to overwrite an existing venv, so a
cold bootstrap against a pre-existing venv must REUSE it (only sync the
requirements) instead of calling `uv venv` — issue fixed in PR #26.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "scripts" / "bootstrap.sh"
REQS = REPO / "requirements.txt"
PYPROJECT = REPO / "orchestrator-backend" / "pyproject.toml"


def _run_bootstrap(tmp: Path, *, venv_exists: bool, marker_ok: bool = False,
                   project: Path | None = None, bootstrap: Path = BOOTSTRAP):
    """Run bootstrap.sh with a fake uv on PATH; return (proc, uv_calls).

    The script runs with `project` (default: an empty dir under tmp) as its
    working directory, so the repo's own .claude/settings.local.json is never
    read by accident."""
    fake_bin = tmp / "bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    call_log = tmp / "uv-calls.log"
    (fake_bin / "uv").write_text(
        f'#!/usr/bin/env bash\necho "$@" >> "{call_log}"\necho "$PWD" >> "{tmp / "uv-cwd.log"}"\nexit 0\n')
    (fake_bin / "uv").chmod(0o755)

    venv = tmp / "venv"
    if venv_exists:
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        py = venv / "bin" / "python"
        py.write_text("#!/usr/bin/env bash\nexit 0\n")
        py.chmod(0o755)
    if marker_ok:
        # what bootstrap.sh writes: SHA-256 over requirements.txt then the backend's pyproject.toml
        want = hashlib.sha256(REQS.read_bytes() + PYPROJECT.read_bytes()).hexdigest()
        venv.mkdir(parents=True, exist_ok=True)
        (venv / ".provledger-reqs.sha256").write_text(want)

    env = dict(
        os.environ,
        PATH=f"{fake_bin}:{os.environ['PATH']}",
        PROVLEDGER_VENV=str(venv),
        PROVLEDGER_BOOTSTRAP_LOG=str(tmp / "bootstrap.log"),
    )
    env.pop("PROVLEDGER_BOOTSTRAP_INSTALLER", None)
    if project is None:
        project = tmp / "project"
    project.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["bash", str(bootstrap)], cwd=str(project),
                          capture_output=True, text=True, env=env)
    calls = call_log.read_text().splitlines() if call_log.exists() else []
    return proc, calls


def test_cold_fresh_creates_venv_and_installs(tmp_path):
    proc, calls = _run_bootstrap(tmp_path, venv_exists=False)
    assert proc.returncode == 0, proc.stderr
    assert any(c.startswith("venv ") for c in calls), calls
    assert any("pip install" in c for c in calls), calls
    assert "venv ready" in proc.stdout


def test_existing_venv_is_reused_not_recreated(tmp_path):
    """The PR #26 regression: uv>=0.5 errors on `uv venv <existing>`, so an
    existing venv must be reused — requirements synced, NO venv creation."""
    proc, calls = _run_bootstrap(tmp_path, venv_exists=True)
    assert proc.returncode == 0, proc.stderr
    assert not any(c.startswith("venv ") for c in calls), calls
    assert any("pip install" in c for c in calls), calls
    assert "venv ready" in proc.stdout


def test_warm_marker_is_a_noop(tmp_path):
    proc, calls = _run_bootstrap(tmp_path, venv_exists=True, marker_ok=True)
    assert proc.returncode == 0, proc.stderr
    assert calls == [], calls
    assert "up to date" in proc.stdout


def test_bootstrap_warns_when_superpowers_and_provledger_both_enabled(tmp_path, monkeypatch):
    """provledger bundles local variants of six superpowers skills; when both
    plugins are enabled the user must be told how to let ours win per project."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        '{"enabledPlugins": {"superpowers@claude-plugins-official": true, '
        '"provledger@provledger": true}}')
    monkeypatch.setenv("HOME", str(home))
    proc, _ = _run_bootstrap(tmp_path, venv_exists=True, marker_ok=True)
    assert proc.returncode == 0, proc.stderr
    assert "same-named skills" in proc.stdout, proc.stdout
    assert "claude plugin disable superpowers@claude-plugins-official --scope local" in proc.stdout


def test_bootstrap_silent_when_project_disables_superpowers(tmp_path, monkeypatch):
    """Once the user follows the notice (`claude plugin disable ... --scope local`)
    the project's .claude/settings.local.json turns superpowers off there, and
    the notice must stop firing on every SessionStart."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        '{"enabledPlugins": {"superpowers@claude-plugins-official": true, '
        '"provledger@provledger": true}}')
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(
        '{"enabledPlugins": {"superpowers@claude-plugins-official": false}}')
    monkeypatch.setenv("HOME", str(home))
    proc, _ = _run_bootstrap(tmp_path, venv_exists=True, marker_ok=True, project=project)
    assert proc.returncode == 0, proc.stderr
    assert "same-named skills" not in proc.stdout, proc.stdout


def _plugin_copy(tmp: Path) -> Path:
    """A copy of the plugin root holding only what bootstrap.sh reads, so a
    test can change one of those files without touching the repo."""
    root = tmp / "plugin"
    (root / "scripts").mkdir(parents=True)
    (root / "orchestrator-backend").mkdir()
    shutil.copy2(BOOTSTRAP, root / "scripts" / "bootstrap.sh")
    shutil.copy2(REQS, root / "requirements.txt")
    shutil.copy2(PYPROJECT, root / "orchestrator-backend" / "pyproject.toml")
    return root


def test_a_backend_pyproject_change_is_not_a_warm_noop(tmp_path):
    """requirements.txt installs the backend editable (`-e ./orchestrator-backend`),
    so the backend's pyproject.toml is an install input too. A version bump or a
    new console script with requirements.txt untouched used to be a warm no-op:
    the venv this was found in still carried 0.1.0 metadata and had no
    `provledger` script long after the release that added it."""
    root = _plugin_copy(tmp_path)
    boot = root / "scripts" / "bootstrap.sh"
    calls_log = tmp_path / "uv-calls.log"

    proc, calls = _run_bootstrap(tmp_path, venv_exists=True, bootstrap=boot)
    assert proc.returncode == 0, proc.stderr
    assert any("pip install" in c for c in calls), calls

    calls_log.unlink()
    proc, calls = _run_bootstrap(tmp_path, venv_exists=True, bootstrap=boot)
    assert proc.returncode == 0 and calls == [] and "up to date" in proc.stdout, (proc.stdout, calls)

    pyproject = root / "orchestrator-backend" / "pyproject.toml"
    bumped = re.sub(r'^version = ".*"$', 'version = "99.0.0"', pyproject.read_text(), count=1, flags=re.M)
    assert bumped != pyproject.read_text()
    pyproject.write_text(bumped)
    proc, calls = _run_bootstrap(tmp_path, venv_exists=True, bootstrap=boot)
    assert proc.returncode == 0, proc.stderr
    assert any("pip install" in c for c in calls), "a backend version bump must re-install the editable backend"
    assert "venv ready" in proc.stdout

    calls_log.unlink()
    proc, calls = _run_bootstrap(tmp_path, venv_exists=True, bootstrap=boot)
    assert calls == [] and "up to date" in proc.stdout, "and the run after it is warm again"


def test_the_install_runs_from_the_plugin_root_not_the_session_cwd(tmp_path):
    """SessionStart runs bootstrap.sh with the user's project as its cwd, and
    uv (like pip) resolves `-e ./orchestrator-backend` in requirements.txt
    against the cwd, not against the requirements file — from any project but
    the plugin itself the install failed with "Distribution not found at
    file://<project>/orchestrator-backend". The same-named-skill notice still
    reads the PROJECT's .claude/settings.local.json, so only the install moves."""
    proc, calls = _run_bootstrap(tmp_path, venv_exists=True)
    assert proc.returncode == 0, proc.stderr
    assert any("pip install" in c for c in calls), calls
    cwds = set((tmp_path / "uv-cwd.log").read_text().split())
    assert cwds == {str(REPO)}, cwds
