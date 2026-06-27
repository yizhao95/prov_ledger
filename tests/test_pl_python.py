import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PL = ROOT / "scripts" / "pl-python"


def test_uses_unified_venv_python(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    fake = venv / "bin" / "python"
    fake.write_text('#!/usr/bin/env bash\necho FAKE_VENV_PY "$@"\n')
    fake.chmod(0o755)
    env = {**os.environ, "PROVLEDGER_VENV": str(venv)}
    r = subprocess.run(["bash", str(PL), "-c", "ignored"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "FAKE_VENV_PY" in r.stdout


def test_falls_back_to_system_python3_with_warning(tmp_path):
    venv = tmp_path / "nonexistent-venv"
    env = {**os.environ, "PROVLEDGER_VENV": str(venv)}
    r = subprocess.run(["bash", str(PL), "-c", "print('hi')"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "hi"
    assert "warning" in r.stderr.lower()
