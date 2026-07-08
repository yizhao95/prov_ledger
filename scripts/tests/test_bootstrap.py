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
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "scripts" / "bootstrap.sh"
REQS = REPO / "requirements.txt"


def _run_bootstrap(tmp: Path, *, venv_exists: bool, marker_ok: bool = False):
    """Run bootstrap.sh with a fake uv on PATH; return (proc, uv_calls)."""
    fake_bin = tmp / "bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    call_log = tmp / "uv-calls.log"
    (fake_bin / "uv").write_text(
        f'#!/usr/bin/env bash\necho "$@" >> "{call_log}"\nexit 0\n')
    (fake_bin / "uv").chmod(0o755)

    venv = tmp / "venv"
    if venv_exists:
        (venv / "bin").mkdir(parents=True)
        py = venv / "bin" / "python"
        py.write_text("#!/usr/bin/env bash\nexit 0\n")
        py.chmod(0o755)
    if marker_ok:
        want = hashlib.sha256(REQS.read_bytes()).hexdigest()
        venv.mkdir(parents=True, exist_ok=True)
        (venv / ".provledger-reqs.sha256").write_text(want)

    env = dict(
        os.environ,
        PATH=f"{fake_bin}:{os.environ['PATH']}",
        PROVLEDGER_VENV=str(venv),
        PROVLEDGER_BOOTSTRAP_LOG=str(tmp / "bootstrap.log"),
    )
    env.pop("PROVLEDGER_BOOTSTRAP_INSTALLER", None)
    proc = subprocess.run(["bash", str(BOOTSTRAP)],
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
