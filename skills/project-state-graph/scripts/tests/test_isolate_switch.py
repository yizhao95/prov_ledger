"""Phase 7 Task 0: `analyzer --isolate {thread,subprocess}` reaches
providers.run_provider through history.snapshot_run (thread by default)."""
from __future__ import annotations

from pathlib import Path

import pytest

from analyzer import cli
from analyzer._host import providers as _providers

ROOT = Path(__file__).resolve().parents[4]
REPO = ROOT / "examples" / "phantom-uplift"


def _capture(monkeypatch):
    seen: list[str] = []
    real = _providers.run_provider

    def spy(provider, ctx, timeout_s=30.0, isolate="thread"):
        seen.append(isolate)
        return real(provider, ctx, timeout_s=timeout_s, isolate=isolate)
    monkeypatch.setattr(_providers, "run_provider", spy)
    return seen


def test_snapshot_run_defaults_to_thread_isolation(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    cli.run(str(REPO), "iso", str(tmp_path / "g.db"), build_cards=False)
    assert seen and set(seen) == {"thread"}


def test_cli_isolate_subprocess_reaches_run_provider(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    rc = cli.main([str(REPO), "--project", "iso", "--db-path", str(tmp_path / "g.db"), "--no-cards",
                   "--isolate", "subprocess"])
    assert rc == 0
    assert seen and set(seen) == {"subprocess"}


def test_cli_rejects_an_unknown_isolate_mode(tmp_path):
    with pytest.raises(SystemExit):
        cli.main([str(REPO), "--project", "iso", "--db-path", str(tmp_path / "g.db"), "--isolate", "container"])
