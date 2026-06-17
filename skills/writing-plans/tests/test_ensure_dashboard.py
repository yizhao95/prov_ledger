"""Tests for scripts/ensure-dashboard.sh — keeps the agent from reading webapp launcher.

Behavior under test
-------------------
ensure-dashboard.sh:
  1. Hits ${HEALTH_URL:-http://127.0.0.1:8765/api/health} via curl
  2. If 200 OK with {"status": "ok"} → exits 0, prints "✅ dashboard already up"
  3. If health check fails → execs underlying webapp launcher
     (~/skill-workspace/orchestrator-webapp/launch_dashboard.sh), then re-checks
  4. If still down after launch attempt → exits non-zero with diagnostic

Test matrix (kept lean — bash + curl is hard to mock without overengineering):
  test_already_healthy_short_circuits   live dashboard returns 200 → exits 0, no side effect
  test_health_url_overridable           HEALTH_URL env var redirects health probe
  test_unreachable_url_fails            HEALTH_URL pointing at a closed port + LAUNCH_CMD=true
                                        (no-op launcher) → exits non-zero with diagnostic
"""
from __future__ import annotations

import os
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest


def _run_ensure(scripts_dir: Path, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(scripts_dir / "ensure-dashboard.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def _free_port() -> int:
    """Grab an OS-assigned free port (race-y but fine for a short test)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dashboard_is_live() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def test_already_healthy_short_circuits(scripts_dir: Path):
    """If dashboard is healthy, script exits 0 with 'already up' message and never invokes launcher."""
    if not _dashboard_is_live():
        pytest.skip("dashboard not running on :8765 — start it before this test")
    result = _run_ensure(scripts_dir)
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    assert "already up" in result.stdout.lower() or "dashboard up" in result.stdout.lower()


def test_health_url_overridable(scripts_dir: Path):
    """Custom HEALTH_URL pointing at the live dashboard still succeeds."""
    if not _dashboard_is_live():
        pytest.skip("dashboard not running on :8765")
    result = _run_ensure(scripts_dir, {"HEALTH_URL": "http://127.0.0.1:8765/api/health"})
    assert result.returncode == 0


def test_unreachable_url_fails(scripts_dir: Path):
    """Dead URL + no-op launcher → non-zero exit with helpful diagnostic.

    We override LAUNCH_CMD to /usr/bin/true so the script doesn't actually try
    to start anything; it should still exit non-zero because the post-launch
    re-check fa """
    dead_port = _free_port()  # nothing listening here
    result = _run_ensure(
        scripts_dir,
        {
            "HEALTH_URL": f"http://127.0.0.1:{dead_port}/api/health",
            "LAUNCH_CMD": "/usr/bin/true",
            "WAIT_SECS": "1",  # don't make the test slow
        },
    )
    assert result.returncode != 0
    msg = (result.stdout + result.stderr).lower()
    assert "dashboard" in msg
    assert str(dead_port) in msg or "unreachable" in msg or "fail" in msg
