import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "scripts" / "bootstrap.sh"


def _run(env_extra, tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), **env_extra}
    return subprocess.run(["bash", str(BOOTSTRAP)], env=env,
                          capture_output=True, text=True)


def test_cold_run_invokes_installer_and_writes_marker(tmp_path):
    venv = tmp_path / "venv"
    sentinel = tmp_path / "installer-ran"
    # Stub installer: just touch a sentinel + create the venv dir; no network.
    installer = f'mkdir -p "{venv}/bin" && touch "{sentinel}"'
    r = _run({"PROVLEDGER_VENV": str(venv),
              "PROVLEDGER_BOOTSTRAP_INSTALLER": installer}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert sentinel.exists(), "installer was not invoked on cold run"
    marker = venv / ".provledger-reqs.sha256"
    expected = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
    assert marker.read_text().strip() == expected


def test_warm_run_skips_installer(tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir(parents=True)
    expected = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
    (venv / ".provledger-reqs.sha256").write_text(expected)
    sentinel = tmp_path / "installer-ran"
    installer = f'touch "{sentinel}"'
    r = _run({"PROVLEDGER_VENV": str(venv),
              "PROVLEDGER_BOOTSTRAP_INSTALLER": installer}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert not sentinel.exists(), "installer ran despite matching marker"
