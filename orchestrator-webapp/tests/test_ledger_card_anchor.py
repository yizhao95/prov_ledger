"""`GET /ledger/card` prints the same anchor line as the CLI (DP phase 3, Task 3).

Two surfaces render the same evidence card, and the dashboard is read-only, so
the page must not have its own opinion about integrity. Both resolve the repo
from the registry and both print one `git anchor:` line — the note sha and the
commit when there is an anchor, the reason when there is not.
"""
from __future__ import annotations

import importlib
import os
import re
import sqlite3
import subprocess

import pytest

from test_routes import _seed_db, _seed_reasons_and_constraints, _seed_state_graph  # noqa: E402

QUESTION = "why must load_orders keep paid orders only?"


def _git(d, *args):
    subprocess.run(["git", *args], cwd=str(d), check=True, capture_output=True)


@pytest.fixture
def anchored_client(tmp_path, monkeypatch):
    """The registered repo is a real git work tree carrying one real anchor."""
    dbp = tmp_path / "orch.db"
    _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    _, reg = _seed_state_graph(tmp_path)            # the registry points `demo` at tmp_path
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    _seed_reasons_and_constraints(dbp)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "anchor-me.txt").write_text("one\n")
    _git(tmp_path, "add", "anchor-me.txt")
    _git(tmp_path, "commit", "-qm", "one")
    from provledger import integrity
    conn = sqlite3.connect(str(dbp))
    conn.row_factory = sqlite3.Row
    note = integrity.anchor_heads(tmp_path, integrity.anchor_payload(conn, plan_id="P1"))
    commit = integrity.head_commit(tmp_path)
    conn.close()
    from app import main, queries
    importlib.reload(queries)
    importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    return c, note, commit


def test_the_card_route_prints_the_anchor_the_registry_repo_carries(anchored_client):
    client, note, commit = anchored_client
    page = client.get(f"/ledger?q={QUESTION}&project=demo").text
    link = re.search(r'href="(/ledger/card\?ask_id=\d+)"', page)
    assert link, "no [Export card] link"
    r = client.get(link.group(1))
    assert r.status_code == 200
    anchor = [ln for ln in r.text.splitlines() if ln.startswith("- git anchor:")]
    assert len(anchor) == 1, r.text
    assert f"git note {note[:12]} @ {commit[:12]}" in anchor[0]
    assert "plan P1" in anchor[0]
    assert "not anchored" not in r.text
    # the narrow claim travels with the card, on the same page as the anchor
    assert "not a claim that what they say happened" in r.text


def test_the_page_and_the_library_agree_on_the_anchor_line(anchored_client):
    client, note, _commit = anchored_client
    page = client.get(f"/ledger?q={QUESTION}&project=demo").text
    link = re.search(r'href="(/ledger/card\?ask_id=\d+)"', page)
    md = client.get(link.group(1)).text
    from provledger.ask import card
    conn = sqlite3.connect(os.environ["ORCH_DB"])
    conn.row_factory = sqlite3.Row
    integ = card.integrity(conn, project="demo")
    conn.close()
    assert integ["anchor"]["note_sha"] == note
    assert card.anchor_text(integ) in md
