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


def vocab_ui(key):
    from app import vocab
    return vocab.ui(key)

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
    assert f"{NOISE} {vocab_ui('unchanged_matches')}" in t or f"{NOISE}" in t
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
    assert vocab_ui("no_match") in r.text          # says so, never a blank panel


def test_every_chip_is_offered_on_the_page(client):
    t = client.get(f"/node/demo/{QN}").text
    for chip in ("all", "adopted", "since:7d", "since:30d", "tier:stated"):
        assert f'data-chip="{chip}"' in t


# ── the rest of the page survives the diet ───────────────────────────────────

def test_space_collapses_to_one_header_line(client):
    t = client.get(f"/node/demo/{QN}").text
    assert 'data-panel="node-head"' in t
    assert "upstream 2" in t and "downstream 1" in t
    assert 'data-disclose="space"' in t              # the lists are behind a disclosure, not gone
    assert "pkg.m.main" in t                          # …and still in the DOM


def test_there_is_exactly_one_timeline(client):
    t = client.get(f"/node/demo/{QN}").text
    assert t.count('data-timeline>') + t.count('data-timeline ') == 1


def test_the_constraints_aside_keeps_its_counts(client):
    t = client.get(f"/node/demo/{QN}").text
    assert 'data-panel="constraints"' in t
    assert "load_orders 只保留已付款的订单" in t
    # DP 2d (Task 3d): the counts read as sentences now; the numbers themselves
    # stayed machine-readable in data-shown-* / data-adopted
    assert "Surfaced" in t and 'data-shown-plan="' in t


def test_the_recorded_words_ride_with_the_moment_they_explain(client):
    t = client.get(f"/node/demo/{QN}").text
    assert "上游说 v2 之后没有 discount 列了" in t
    assert 'data-verbatim="1"' in t


# ── DP phase 2d follow-up: 16 rows saying the same sentence is not a ledger ──
# The real compute_etag page was 21 rows, 16 of them the identical derived
# "covered by active constraint #1414" — one per plan that met the node. The
# rows stay (append-only); the PAGE groups them.

def _seed_repeats(dbp, n=16, project="demo"):
    """n identical derived records on one node, as R5 used to write them."""
    conn = odb.open_db(dbp)
    for i in range(n):
        conn.execute("INSERT INTO change_reason (project, plan_id, node_key, kind, role, interpretation, "
                     "occurred_at, recorded_at, recorded_by, tier, rule_id, hash) "
                     "VALUES (?, ?, 'nk_a', 'technical', 'reason', 'covered by active constraint #1414: keep paid orders only', "
                     "?, ?, 'system', 'derived', 'R5', ?)",
                     (project, f"P{i}", f"2026-09-{15 + i % 2:02d} 10:00:00", f"2026-09-{15 + i % 2:02d} 10:00:00", f"h-rep-{i}"))
    conn.commit(); conn.close()


def test_identical_records_collapse_into_one_row_with_a_count(client):
    _seed_repeats(client._db)
    led = _ledger(client, significant_only=True)
    groups = led["groups"]
    rep = [g for g in groups if g["count"] > 1]
    assert rep, "16 identical records did not group"
    g = rep[0]
    assert g["count"] == 16 and g["tier"] == "derived"
    assert "covered by" in (g["text"] or "")
    assert g["first_at"] and g["last_at"] and g["first_at"] <= g["last_at"]
    assert len(g["rows"]) == 16, "the individual records must still be reachable"


def test_the_grouped_row_is_rendered_once_with_its_span(client):
    _seed_repeats(client._db)
    t = client.get("/node/demo/pkg.m.load_orders").text
    tl = t[t.index('data-timeline'):]
    assert tl.count("covered by active constraint #1414") == 1, "the sentence is still repeated in the timeline"
    assert 'data-group-count="16"' in t
    assert "16 plans" in t or "16 " in t


def test_the_page_is_under_the_row_budget(client):
    """The acceptance the user set: at most 8 visible rows plus fold counts."""
    _seed_repeats(client._db)
    t = client.get("/node/demo/pkg.m.load_orders").text
    assert t.count('data-tl-row') <= 8, f"{t.count('data-timeline-row')} rows still rendered"


def test_recent_lists_one_of_each_kind_not_the_same_sentence_eight_times(client):
    _seed_repeats(client._db)
    from app import queries
    conn = queries.open_db_readonly(client._db)
    try:
        led = queries.get_node_ledger(conn, "demo", QN)
    finally:
        conn.close()
    strip = queries.trace_strip(led)
    assert len(strip) <= 5
    # structural events carry no record text; what must not repeat is a SENTENCE
    texts = [t for t in ((r.get("record") or {}).get("text") for r in strip) if t]
    assert len(texts) == len(set(texts)), f"Recent repeats itself: {texts}"
    kinds = [queries._recent_kind(r) for r in strip]
    assert len(kinds) == len(set(kinds)), f"Recent shows one kind twice: {kinds}"


def test_downstream_consumers_are_in_the_header_not_behind_a_disclosure(client):
    """"Will this break something" is the header's job; only-upstream folds."""
    t = client.get(f"/node/demo/{QN}").text
    head = t[t.index('data-panel="node-head"'):t.index('data-panel="trace-strip"')]
    assert "pkg.m.clean" in head, "the downstream consumer is not in the header"
    assert 'data-downstream="1"' in head
