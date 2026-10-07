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


def test_default_launcher_is_the_bundled_webapp(scripts_dir: Path):
    """FL-012: the default LAUNCH_CMD used to point at
    ~/skill-workspace/orchestrator-webapp/launch_dashboard.sh, which does not
    exist in a plugin install. It must resolve to the bundled webapp launcher
    (relative to the plugin root, or $CLAUDE_PLUGIN_ROOT when set)."""
    result = _run_ensure(scripts_dir, {"ENSURE_DASHBOARD_PRINT_ONLY": "1"})
    assert result.returncode == 0, result.stderr
    line = next(l for l in result.stdout.splitlines() if l.startswith("LAUNCH_CMD="))
    launcher = Path(line.split("LAUNCH_CMD=bash ", 1)[1].strip())
    assert launcher.name == "launch_dashboard.sh" and launcher.is_file(), launcher
    assert launcher.parent.name == "orchestrator-webapp"


def test_plugin_root_env_overrides_launcher_location(scripts_dir: Path, tmp_path: Path):
    root = tmp_path / "plugin"
    (root / "orchestrator-webapp").mkdir(parents=True)
    result = _run_ensure(scripts_dir, {"ENSURE_DASHBOARD_PRINT_ONLY": "1",
                                       "CLAUDE_PLUGIN_ROOT": str(root)})
    assert f"LAUNCH_CMD=bash {root}/orchestrator-webapp/launch_dashboard.sh" in result.stdout


def _health_url(scripts_dir: Path, env: dict) -> str:
    base = {k: v for k, v in os.environ.items() if k not in ("HEALTH_URL", "PROVLEDGER_DASH_PORT")}
    result = subprocess.run(["bash", str(scripts_dir / "ensure-dashboard.sh")], capture_output=True,
                            text=True, env={**base, "ENSURE_DASHBOARD_PRINT_ONLY": "1", **env}, timeout=15)
    assert result.returncode == 0, result.stderr
    return next(l for l in result.stdout.splitlines() if l.startswith("HEALTH_URL=")).split("=", 1)[1]


def test_the_probe_follows_the_launchers_port(scripts_dir: Path):
    """One port, one knob. The launcher starts on $PROVLEDGER_DASH_PORT; probing
    8765 regardless started a dashboard on the new port and then waited on the
    old one — or, on a machine already running a dashboard there, reported that
    one as up."""
    assert _health_url(scripts_dir, {"PROVLEDGER_DASH_PORT": "9911"}) == "http://127.0.0.1:9911/api/health"


def test_an_explicit_health_url_still_wins(scripts_dir: Path):
    url = _health_url(scripts_dir, {"PROVLEDGER_DASH_PORT": "9911",
                                    "HEALTH_URL": "http://127.0.0.1:9922/api/health"})
    assert url == "http://127.0.0.1:9922/api/health"


def test_with_neither_set_the_probe_stays_on_8765(scripts_dir: Path):
    assert _health_url(scripts_dir, {}) == "http://127.0.0.1:8765/api/health"


class _Health:
    """A one-route server answering /api/health with a fixed status and body."""

    def __init__(self, status: int, body: str):
        import http.server
        import threading

        status_, body_ = status, body.encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(status_)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body_)

            def log_message(self, *_):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/health"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_a_dashboard_with_no_ledger_yet_counts_as_up(scripts_dir: Path):
    """Before the first plan creates the ledger, the dashboard answers 503 with its
    own JSON. That is a running dashboard: launching a second one would only
    collide on the port."""
    health = _Health(503, '{"ok": false, "error": "orchestrator.db not found"}')
    try:
        result = _run_ensure(scripts_dir, {"HEALTH_URL": health.url, "LAUNCH_CMD": "/usr/bin/false",
                                           "WAIT_SECS": "1"})
    finally:
        health.close()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "launching" not in result.stdout


def test_a_503_that_is_not_the_dashboard_still_counts_as_down(scripts_dir: Path):
    health = _Health(503, "Service Unavailable")
    try:
        result = _run_ensure(scripts_dir, {"HEALTH_URL": health.url, "LAUNCH_CMD": "/usr/bin/true",
                                           "WAIT_SECS": "1"})
    finally:
        health.close()
    assert result.returncode != 0
    assert "launching" in result.stdout


def test_the_failure_names_the_launchers_log(scripts_dir: Path):
    result = _run_ensure(scripts_dir, {"HEALTH_URL": f"http://127.0.0.1:{_free_port()}/api/health",
                                       "LAUNCH_CMD": "/usr/bin/true", "WAIT_SECS": "1",
                                       "PROVLEDGER_DASH_LOG": "/tmp/some-dash.log"})
    assert result.returncode != 0
    assert "/tmp/some-dash.log" in result.stderr
