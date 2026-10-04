"""launch_dashboard.sh against a ledger that does not exist yet.

Before the first plan there is no orchestrator.db, so `/api/health` answers 503.
The launcher used to read that as "did not start", exit 1 with the server it had
just started still running, and then report "port in use" on the next run. A
dashboard with no ledger yet is running: it shows the empty state and picks the
ledger up the moment the first plan is recorded. These tests run the real script
on a free port against a temp ORCH_DB, and always stop what they started.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

WEBAPP = Path(__file__).resolve().parents[1]
LAUNCHER = WEBAPP / "launch_dashboard.sh"
# the venv running this suite (not resolved: a venv's python is a symlink to the base interpreter)
VENV = Path(sys.executable).parent.parent if (Path(sys.executable).parent / "uvicorn").exists() \
    else Path(os.environ.get("PROVLEDGER_VENV", Path.home() / "skill-workspace" / ".venv"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pids_on(port: int) -> list[int]:
    r = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True)
    return [int(p) for p in r.stdout.split()]


def _stop(port: int) -> None:
    for pid in _pids_on(port):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(50):
        if not _pids_on(port):
            return
        time.sleep(0.1)
    for pid in _pids_on(port):
        os.kill(pid, signal.SIGKILL)


@pytest.fixture
def env(tmp_path):
    if not (VENV / "bin" / "uvicorn").exists():
        pytest.skip(f"no uvicorn in {VENV}")
    for tool in ("curl", "lsof"):
        if subprocess.run(["which", tool], capture_output=True).returncode:
            pytest.skip(f"{tool} not installed")
    port = _free_port()
    e = {**os.environ,
         "ORCH_DB": str(tmp_path / "not-yet" / "orchestrator.db"),
         "PSG_REGISTRY_ROOT": str(tmp_path / "registry"),
         "PROVLEDGER_DASH_PORT": str(port),
         "PROVLEDGER_DASH_LOG": str(tmp_path / "server.log"),
         "PROVLEDGER_DASH_WAIT": "30",
         "PROVLEDGER_VENV": str(VENV)}
    e.pop("PROVLEDGER_WEBAPP_DIR", None)
    yield e
    _stop(port)


def _launch(e: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(LAUNCHER)], env=e, capture_output=True, text=True, timeout=60)


def test_no_ledger_yet_is_running_not_failed(env):
    r = _launch(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no ledger yet" in r.stdout
    assert env["ORCH_DB"] in r.stdout, "the message should say which ledger it is waiting for"
    assert _pids_on(int(env["PROVLEDGER_DASH_PORT"])), "the server should still be running"


def test_the_second_run_finds_it_running_instead_of_port_in_use(env):
    assert _launch(env).returncode == 0
    r = _launch(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "in use" not in r.stdout
    assert "already running" in r.stdout and "no ledger yet" in r.stdout


def test_a_port_held_by_something_else_is_still_refused(env, tmp_path):
    """The 503 rule must not swallow the "don't kill what we don't own" guard."""
    port = env["PROVLEDGER_DASH_PORT"]
    other = subprocess.Popen([sys.executable, "-m", "http.server", port, "--bind", "127.0.0.1"],
                             cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if _pids_on(int(port)):
                break
            time.sleep(0.1)
        r = _launch(env)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "in use" in r.stdout
    finally:
        other.terminate()
        other.wait(timeout=10)
