"""DP phase 2d (Task 3b): the node page stops being a wall.

2b put space, time and intent in three columns and printed every event of every
run. On a real node that is hundreds of rows, most of them `node_matched` —
"the analyser recognised this node again", which is true and says nothing.

So: one column, one timeline, and only the moments where something actually
changed. The noise is FOLDED, never dropped — a folded row carries its count,
because a count is the difference between "nothing happened" and "we stopped
showing you". Chips narrow the timeline and live in the URL so a filtered view
is a link you can send someone.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ORCH_BACKEND = REPO / "orchestrator-backend"
WEBAPP = REPO / "orchestrator-webapp"
sys.path.insert(0, str(WEBAPP))
sys.path.insert(0, str(ORCH_BACKEND))
sys.path.insert(0, str(ORCH_BACKEND / "tests"))
sys.path.insert(0, str(REPO / "orchestrator-webapp" / "tests"))

import _psg_schema as ps  # noqa: E402
from orchestrator import db as odb  # noqa: E402
from test_routes import _seed_db  # noqa: E402

QN = "pkg.m.load_orders"
SIGNIFICANT = 5      # added, renamed, changed, column_dropped, identity_asserted
NOISE = 12           # node_matched / identity_kept


def _graph(tmp_path, project="demo"):
    """One node, 17 events: 5 that changed something and 12 that recognised it."""
    path = tmp_path / f"{project}-state-graph.db"
    c = ps.build(path)
    seq = 0
    ps.add_run(c, 1, plan_id="P0", sha="aaaaaaa")
    ps.add_snapshot(c, 1, "nk_a", "pkg.m.load")
    ps.add_event(c, 1, 1, "node_added", "nk_a", '{"qualified_name": "pkg.m.load"}')
    for run in range(2, 14):                       # 12 quiet runs
        ps.add_run(c, run, plan_id=f"P{run}", sha=f"sha{run:04d}")
        ps.add_snapshot(c, run, "nk_a", QN if run > 2 else "pkg.m.load")
        ps.add_event(c, run, 1, "node_matched" if run % 2 else "identity_kept", "nk_a", '{"via": "struct_sig"}')
    ps.add_run(c, 14, plan_id="P14", sha="bbbbbbb")
    ps.add_snapshot(c, 14, "nk_a", QN, struct_sig="s2")
    ps.add_event(c, 14, 1, "node_renamed", "nk_a", '{"from": "pkg.m.load", "to": "pkg.m.load_orders"}')
    ps.add_event(c, 14, 2, "node_changed", "nk_a", '{"changed": ["struct_sig"], "before": "a, b, c", "after": "a, b"}')
    ps.add_event(c, 14, 3, "column_dropped", "nk_a", '{"column": "discount"}')
    ps.add_event(c, 14, 4, "identity_asserted", "nk_a", '{"cur": "pkg.m.load_orders", "evidence": "main() calls it", "arbiter": "x"}', tier="asserted")
    c.executescript(f"""
        CREATE TABLE IF NOT EXISTS consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
        INSERT INTO node_type (id, name) VALUES (1, 'function');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key)
             VALUES (7, 1, 'load_orders', '{QN}', 'pkg/m.py', 14, 'nk_a');
        INSERT INTO consistency_card VALUES (7, '{{"callers": ["pkg.m.main", "pkg.m.cli"], "callees": ["pd.read_csv"], "output_consumers": ["pkg.m.clean"], "reads": ["orders"], "writes": []}}');
    """)
    c.commit(); c.close()
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": project, "repo": str(tmp_path), "db_path": str(path), "commit_sha": "bbbbbbb"}]}))
    return str(path), reg


def _seed_records(dbp, project="demo"):
    from orchestrator import constraints as oc, provenance as pv
    conn = odb.open_db(dbp)
    u = pv.insert_utterance(conn, session_id="s", project=project, plan_id="P14",
                            text="上游说 v2 之后没有 discount 列了", occurred_at="2026-09-15 10:00:00")
    pv.insert_reason(conn, project=project, plan_id="P14", node_key="nk_a", kind="technical", run_id=14,
                     verbatim=(u, 0, len("上游说 v2 之后没有 discount 列了")), recorded_by="agent")
    oc.record_constraint(conn, project=project, subjects=["nk_a"], statement="load_orders 只保留已付款的订单",
                         rationale="财务按已付款对账", why_ref="docs/finance.md", why_visibility="shared")
    conn.commit(); conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    _, reg = _graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    _seed_records(dbp)
    from app import queries, main
    importlib.reload(queries); importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app); c._db = dbp
    return c


def _ledger(client, **kw):
    from app import queries
    conn = queries.open_db_readonly(client._db)
    try:
        return queries.get_node_ledger(conn, "demo", QN, **kw)
    finally:
        conn.close()


# ── what the timeline keeps and what it folds ────────────────────────────────

def test_the_default_timeline_only_carries_moments_that_changed_something(client):
    led = _ledger(client, significant_only=True)
    kinds = {e["event_type"] for e in led["timeline"] if e.get("event_type")}
    assert "node_matched" not in kinds and "identity_kept" not in kinds
    assert {"node_added", "node_renamed", "node_changed", "column_dropped", "identity_asserted"} <= kinds


def test_the_folded_noise_is_counted_never_dropped(client):
    led = _ledger(client, significant_only=True)
    assert led["folded"]["events"] == NOISE, "the quiet runs must be counted, not forgotten"
    t = client.get(f"/node/demo/{QN}").text
    assert f"还有 {NOISE} 次无变化的匹配" in t
    assert 'data-folded="12"' in t


def test_show_all_puts_every_event_back(client):
    led = _ledger(client, significant_only=False)
    kinds = {e["event_type"] for e in led["timeline"] if e.get("event_type")}
    assert "node_matched" in kinds and "identity_kept" in kinds
    t = client.get(f"/node/demo/{QN}?show=all").text
    assert t.count('data-event=') >= SIGNIFICANT + NOISE


def test_the_page_is_at_least_half_as_tall_as_it_was(client):
    """The whole point of the task: fewer rows on screen, same ledger behind."""
    folded = client.get(f"/node/demo/{QN}").text.count('data-event=')
    everything = client.get(f"/node/demo/{QN}?show=all").text.count('data-event=')
    assert folded * 2 <= everything, f"{folded} rows folded vs {everything} unfolded — not half"


# ── chips live in the URL ────────────────────────────────────────────────────

def test_parse_show_round_trips_the_chip_state():
    from app import queries
    s = queries.parse_show("events:node_renamed,tier:stated,since:7d,adopted")
    assert s["events"] == ["node_renamed"] and s["tier"] == ["stated"]
    assert s["since"] == "7d" and s["adopted"] is True and s["all"] is False
    assert queries.parse_show("all")["all"] is True
    assert queries.parse_show(None) == queries.parse_show("")


def test_an_event_chip_narrows_the_timeline(client):
    t = client.get(f"/node/demo/{QN}?show=events:node_renamed").text
    assert 'data-event="node_renamed"' in t and 'data-event="column_dropped"' not in t
    assert 'data-chip="events:node_renamed" data-on="1"' in t


def test_an_unknown_chip_value_is_ignored_rather_than_emptying_the_page(client):
    r = client.get(f"/node/demo/{QN}?show=events:not_an_event")
    assert r.status_code == 200
    assert "没有符合当前筛选的记录" in r.text          # says so, never a blank panel


def test_every_chip_is_offered_on_the_page(client):
    t = client.get(f"/node/demo/{QN}").text
    for chip in ("all", "adopted", "since:7d", "since:30d", "tier:stated"):
        assert f'data-chip="{chip}"' in t


# ── the rest of the page survives the diet ───────────────────────────────────

def test_space_collapses_to_one_header_line(client):
    t = client.get(f"/node/demo/{QN}").text
    assert 'data-panel="node-head"' in t
    assert "上游 2 · 下游 1" in t
    assert 'data-disclose="space"' in t              # the lists are behind a disclosure, not gone
    assert "pkg.m.main" in t                          # …and still in the DOM


def test_there_is_exactly_one_timeline(client):
    t = client.get(f"/node/demo/{QN}").text
    assert t.count('data-timeline') == 1


def test_the_constraints_aside_keeps_its_counts(client):
    t = client.get(f"/node/demo/{QN}").text
    assert 'data-panel="constraints"' in t
    assert "load_orders 只保留已付款的订单" in t
    assert "展示" in t


def test_the_recorded_words_ride_with_the_moment_they_explain(client):
    t = client.get(f"/node/demo/{QN}").text
    assert "上游说 v2 之后没有 discount 列了" in t
    assert 'data-verbatim="1"' in t
