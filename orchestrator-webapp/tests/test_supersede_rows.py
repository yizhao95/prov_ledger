"""The dashboard shows the newest row of a supersede chain (FL-238, FL-239).

A correction is appended, never written over: the old row keeps its words and
points at its successor. The plan's reasons panel, the unstated count and the
node page show the successor, say which row it corrects (`data-corrects` keeps
the raw id), and never the corrected row's words."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))       # the conftest puts the backend and the webapp on the path

from orchestrator import db as odb  # noqa: E402
from test_node_rail import QN, _graph  # noqa: E402
from test_routes import _seed_db  # noqa: E402

QUOTE = "keep compute_etag weekly please"
FAILED = "COMMAND: bash scripts/run_tests.sh backend — exit_code=1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from orchestrator import provenance as pv
    dbp = tmp_path / "orch.db"
    plan = _seed_db(dbp)["plan_id"]
    monkeypatch.setenv("ORCH_DB", str(dbp))
    _, reg = _graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    conn = odb.open_db(dbp)
    conn.execute("UPDATE Plans SET project = 'demo' WHERE plan_id = ?", (plan,))
    u = pv.insert_utterance(conn, session_id="s", project="demo", plan_id=plan, text=QUOTE,
                            occurred_at="2026-09-15 09:00:00")
    old = pv.insert_reason(conn, project="demo", plan_id=plan, node_key="nk_a", kind="technical",
                           verbatim=(u, 0, len(QUOTE)), rule_id="R0", recorded_by="system")
    new = pv.insert_reason(conn, project="demo", plan_id=plan, node_key="nk_a", kind="technical", recorded_by="system")
    pv.supersede(conn, old, new)
    old2 = pv.insert_reason(conn, project="demo", plan_id=plan, node_key="nk_a", kind="technical",
                            role="rejected_path", interpretation=FAILED, rule_id="R6", recorded_by="system")
    new2 = pv.insert_reason(conn, project="demo", plan_id=plan, node_key=None, kind="technical",
                            role="rejected_path", interpretation=FAILED, rule_id="R6", recorded_by="system")
    pv.supersede(conn, old2, new2)
    conn.commit()
    conn.close()
    from app import main, queries
    importlib.reload(queries)
    importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    c.ids = {"plan": plan, "old": old, "new": new, "old2": old2, "new2": new2}
    c.queries = queries
    c.dbp = dbp
    return c


def test_the_plan_reasons_are_the_newest_rows_with_what_they_correct(client):
    conn = odb.open_db(client.dbp)
    rows = {r["id"]: r for r in client.queries.get_node_reasons(conn, client.ids["plan"])}
    assert set(rows) == {client.ids["new"], client.ids["new2"]}
    assert rows[client.ids["new"]]["corrects"] == [client.ids["old"]]
    assert client.queries.get_unstated(conn, client.ids["plan"])["unstated"] == 1
    conn.close()
    page = client.get(f"/plan/{client.ids['plan']}").text
    assert QUOTE not in page and page.count(FAILED) == 1
    assert f'data-corrects="{client.ids["old"]}"' in page and f'corrects #{client.ids["old"]}' in page


def test_the_node_page_does_not_show_a_corrected_row(client):
    page = client.get(f"/node/demo/{QN}").text
    assert QUOTE not in page and FAILED not in page
