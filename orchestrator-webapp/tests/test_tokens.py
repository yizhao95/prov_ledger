"""DP phase 2d (Task 1): one tokens.json, two consumers, no drift.

Claude Design only understands compiled React components, so the dashboard's
look has to exist twice — once in the Jinja templates, once in the component
library. Two hand-maintained copies of a palette drift within a week. So the
palette lives in `app/static/tokens.json` and both sides are GENERATED from it:
`tokens.js` for the templates, `design/src/tokens.ts` for the library. The
generator's `--check` mode is what this suite uses to prove the two generated
files still match their source.
"""
from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ORCH_BACKEND = REPO / "orchestrator-backend"
WEBAPP = REPO / "orchestrator-webapp"
sys.path.insert(0, str(WEBAPP))
sys.path.insert(0, str(ORCH_BACKEND))
sys.path.insert(0, str(REPO / "orchestrator-webapp" / "tests"))

from test_routes import _seed_db  # noqa: E402

GEN = REPO / "scripts" / "gen_tokens.py"
TOKENS_JSON = WEBAPP / "app" / "static" / "tokens.json"
TOKENS_JS = WEBAPP / "app" / "static" / "tokens.js"
TOKENS_TS = WEBAPP / "design" / "src" / "tokens.ts"
BASE_HTML = WEBAPP / "app" / "templates" / "base.html"

HEX = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")
TIERS = ("observed", "derived", "asserted", "stated", "unstated")


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    from app import queries, main
    importlib.reload(queries); importlib.reload(main)
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def _tokens() -> dict:
    return json.loads(TOKENS_JSON.read_text(encoding="utf-8"))


def _check(cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GEN), "--check"], cwd=str(cwd or REPO),
                          capture_output=True, text=True)


# ── the source and its two generated files ───────────────────────────────────

def test_the_generated_files_are_in_sync_with_their_source(tmp_path):
    r = _check()
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_fails_loudly_when_the_source_moves_without_the_generator(tmp_path, monkeypatch):
    """The point of --check: an edit to tokens.json that never ran the generator
    must be caught, not silently half-applied."""
    original = TOKENS_JSON.read_text(encoding="utf-8")
    doc = json.loads(original)
    doc["colors"]["brand-blue"] = "#123456"
    try:
        TOKENS_JSON.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        r = _check()
        assert r.returncode != 0 and "tokens" in (r.stdout + r.stderr)
    finally:
        TOKENS_JSON.write_text(original, encoding="utf-8")
    assert _check().returncode == 0


def test_the_generator_writes_both_consumers(tmp_path):
    assert TOKENS_JS.exists() and TOKENS_TS.exists()
    js = TOKENS_JS.read_text(encoding="utf-8")
    ts = TOKENS_TS.read_text(encoding="utf-8")
    assert "window.PROVLEDGER_TOKENS" in js
    assert "export const tokens" in ts and "as const" in ts
    for name, value in _tokens()["colors"].items():
        assert value in js and value in ts


def test_both_generated_files_say_they_are_generated(tmp_path):
    for p in (TOKENS_JS, TOKENS_TS):
        head = p.read_text(encoding="utf-8")[:400]
        assert "gen_tokens.py" in head and "tokens.json" in head


# ── the Jinja side ───────────────────────────────────────────────────────────

def test_base_html_carries_no_hex_colour_of_its_own(tmp_path):
    """Every colour in the chrome now comes from the tokens file; a hex literal
    back in base.html is exactly the drift this task exists to stop."""
    found = HEX.findall(BASE_HTML.read_text(encoding="utf-8"))
    assert found == [], f"base.html still hard-codes {found}"


def test_base_html_loads_the_tokens_and_feeds_them_to_tailwind(tmp_path):
    html = BASE_HTML.read_text(encoding="utf-8")
    assert "/static/tokens.js" in html
    assert "PROVLEDGER_TOKENS.colors" in html


def test_the_page_serves_the_tokens_and_uses_them(client):
    r = client.get("/")
    assert r.status_code == 200 and "/static/tokens.js" in r.text
    j = client.get("/static/tokens.js")
    assert j.status_code == 200 and "window.PROVLEDGER_TOKENS" in j.text
    assert _tokens()["colors"]["brand-blue"] in j.text


# ── the Python side ──────────────────────────────────────────────────────────

def test_tier_badge_reads_its_five_rows_from_the_tokens_file():
    from app import queries
    tiers = _tokens()["tiers"]
    assert set(tiers) == set(TIERS)
    for tier in TIERS:
        label, classes = queries.tier_badge(tier)
        assert label == tiers[tier]["label"] and classes == tiers[tier]["classes"]
    assert queries.tier_badge(None) == queries.tier_badge("unstated")      # the backstop is still a real tier


def test_the_tokens_file_carries_what_both_sides_need():
    doc = _tokens()
    for section in ("colors", "tiers", "severities", "type_scale", "spacing", "radii"):
        assert section in doc and doc[section], f"tokens.json has no {section}"
    assert set(doc["severities"]) == {"blocking", "warning", "info"}
    for s in doc["severities"].values():
        assert s.get("icon") and s.get("label")
