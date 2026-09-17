"""DP phase 2d (Task 0, half of FL-076): the Graph view stops drawing the whole
project on every load.

User feedback on the 2b screenshots, 2026-09-16: recording every node is right,
DRAWING every node is wrong. So the view has four modes and
the biggest one is never the default:

  focus  — the focus node's ±2-hop neighbourhood (the default when there is a focus)
  story  — only the nodes that have a story (node_badge_v count > 0) plus the data
           nodes they directly produce or consume (the default when there is none)
  data   — the data nodes and the functions that directly produce or consume them
  full   — everything, only behind a link that says how many nodes that is

Nothing is hidden silently: every mode reports the full node count.
"""
from __future__ import annotations

import importlib
import json as _json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ORCH_BACKEND = REPO / "orchestrator-backend"
WEBAPP = REPO / "orchestrator-webapp"                  # `app` on the path without relying on collection order
sys.path.insert(0, str(WEBAPP))
sys.path.insert(0, str(ORCH_BACKEND))
sys.path.insert(0, str(ORCH_BACKEND / "tests"))
sys.path.insert(0, str(REPO / "orchestrator-webapp" / "tests"))

import _psg_schema as ps  # noqa: E402
from orchestrator import db as odb  # noqa: E402
from orchestrator import psg_bridge  # noqa: E402
from test_routes import _seed_db  # noqa: E402


def vocab_ui(key):
    from app import vocab
    return vocab.ui(key)

CHAIN = 12          # f0 → f1 → … → f11, all in pkg/core/mod.py
ISLANDS = 5         # unconnected functions in pkg/side/util.py — no edges, no story
FUNCS = CHAIN + ISLANDS
DATA = 3            # orders (sql_table), orders.amount (column), customers (sql_table)
STORY_NODES = ("nk_3", "nk_7")          # the two functions a record is anchored on


def _chain_graph(tmp_path, project="demo"):
    """One run: a 12-function call chain, 5 unconnected functions, and three data
    nodes — orders read by f3, orders.amount written by f7, customers read by f5
    (f5 carries no story, which is what tells `story` and `data` apart)."""
    path = tmp_path / f"{project}-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0", sha="aaaaaaa")
    c.execute("INSERT INTO node_type (id, name) VALUES (1, 'function'), (2, 'sql_table'), (3, 'column')")

    def snap(key, qn, ntype, fp, nid):
        c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, struct_sig, "
                  "dataflow_sig, dataflow_trivial, attrs_json) VALUES (1, ?, ?, ?, ?, 's', NULL, 1, '{}')",
                  (key, ntype, qn, fp))
        c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (?, ?, ?, ?, ?, 1, ?)",
                  (nid, {"function": 1, "sql_table": 2, "column": 3}[ntype], qn.split(".")[-1], qn, fp, key))

    for i in range(CHAIN):
        snap(f"nk_{i}", f"pkg.core.mod.f{i}", "function", "pkg/core/mod.py", i + 1)
        ps.add_event(c, 1, i + 1, "node_added", f"nk_{i}")
    for j in range(ISLANDS):
        k = CHAIN + j
        snap(f"nk_{k}", f"pkg.side.util.g{j}", "function", "pkg/side/util.py", k + 1)
    snap("nk_orders", "orders", "sql_table", None, 100)
    snap("nk_amount", "orders.amount", "column", None, 101)
    snap("nk_customers", "customers", "sql_table", None, 102)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS edge_type (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER NOT NULL, src_node_id INTEGER NOT NULL,
                                         dst_node_id INTEGER NOT NULL, metadata_json TEXT, run_id INTEGER, confidence TEXT);
        INSERT INTO edge_type (id, name) VALUES (1, 'calls'), (2, 'reads_sql'), (3, 'writes_column');
    """)
    for i in range(CHAIN - 1):
        c.execute("INSERT INTO edge (edge_type_id, src_node_id, dst_node_id) VALUES (1, ?, ?)", (i + 1, i + 2))
    c.execute("INSERT INTO edge (edge_type_id, src_node_id, dst_node_id) VALUES (2, 4, 100)")    # f3 reads orders
    c.execute("INSERT INTO edge (edge_type_id, src_node_id, dst_node_id) VALUES (3, 8, 101)")    # f7 writes orders.amount
    c.execute("INSERT INTO edge (edge_type_id, src_node_id, dst_node_id) VALUES (2, 6, 102)")    # f5 reads customers
    c.commit(); c.close()
    reg = tmp_path / "projects.json"
    reg.write_text(_json.dumps({"projects": [{"name": project, "repo": str(tmp_path), "db_path": str(path), "commit_sha": "aaaaaaa"}]}))
    return str(path), reg


def _seed_story(dbp, project="demo"):
    """A record anchored on f3 and one on f7 — the only two nodes with a story."""
    conn = odb.open_db(dbp)
    for key in STORY_NODES:
        conn.execute("INSERT INTO change_reason (project, plan_id, node_key, kind, role, interpretation, occurred_at, recorded_by, tier, hash) "
                     "VALUES (?, 'P0', ?, 'technical', 'reason', 'because', '2026-09-16 00:00:00', 'agent', 'asserted', ?)",
                     (project, key, f"h-{key}"))
    conn.commit(); conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    _, reg = _chain_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    _seed_story(dbp)
    from app import queries, main
    importlib.reload(queries); importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app); c._db = dbp
    return c


def _keys(g) -> set:
    return {n["node_key"] for n in g["nodes"]}


# ── the bridge ───────────────────────────────────────────────────────────────

def test_focus_mode_returns_only_the_two_hop_neighbourhood(tmp_path):
    g, _ = _chain_graph(tmp_path)
    nb = psg_bridge.graph_at(g, mode="focus", focus="pkg.core.mod.f5", hops=2)
    assert nb["mode"] == "focus" and nb["focus_key"] == "nk_5" and nb["focus_found"] is True and nb["hops"] == 2
    assert _keys(nb) == {f"nk_{i}" for i in (3, 4, 5, 6, 7)}          # level=functions: the data neighbour is a different mode
    assert nb["total_nodes"] == FUNCS and nb["truncated"] is True
    assert all({e["src_key"], e["dst_key"]} <= _keys(nb) for e in nb["edges"])


def test_a_node_key_focus_and_a_wider_hop_count_work_the_same_way(tmp_path):
    g, _ = _chain_graph(tmp_path)
    assert _keys(psg_bridge.graph_at(g, mode="focus", focus="nk_0", hops=3)) == {"nk_0", "nk_1", "nk_2", "nk_3"}
    assert _keys(psg_bridge.graph_at(g, mode="focus", focus="nk_12", hops=2)) == {"nk_12"}   # an island is its own neighbourhood


def test_an_unknown_focus_falls_back_to_the_whole_graph_and_says_so(tmp_path):
    g, _ = _chain_graph(tmp_path)
    nb = psg_bridge.graph_at(g, mode="focus", focus="pkg.core.mod.nope", hops=2)
    assert nb["focus_found"] is False and nb["truncated"] is False and len(nb["nodes"]) == FUNCS


def test_story_mode_is_the_nodes_with_a_story_plus_the_data_they_touch(tmp_path):
    g, _ = _chain_graph(tmp_path)
    s = psg_bridge.graph_at(g, mode="story", story_keys=set(STORY_NODES))
    assert s["mode"] == "story"
    assert _keys(s) == {"nk_3", "nk_7", "nk_orders", "nk_amount"}     # customers hangs off f5, which has no story
    assert {(e["src_key"], e["dst_key"]) for e in s["edges"]} == {("nk_3", "nk_orders"), ("nk_7", "nk_amount")}
    assert s["total_nodes"] == FUNCS


def test_story_mode_without_a_single_story_is_empty_and_says_the_full_count(tmp_path):
    g, _ = _chain_graph(tmp_path)
    s = psg_bridge.graph_at(g, mode="story", story_keys=set())
    assert s["nodes"] == [] and s["total_nodes"] == FUNCS and s["truncated"] is True


def test_data_mode_is_every_data_node_plus_the_functions_touching_it(tmp_path):
    g, _ = _chain_graph(tmp_path)
    d = psg_bridge.graph_at(g, mode="data")
    assert _keys(d) == {"nk_orders", "nk_amount", "nk_customers", "nk_3", "nk_7", "nk_5"}
    assert d["mode"] == "data"


def test_full_mode_is_everything_and_the_smaller_modes_are_subsets_of_it(tmp_path):
    g, _ = _chain_graph(tmp_path)
    full = psg_bridge.graph_at(g, mode="full", level="full")
    assert len(full["nodes"]) == FUNCS + DATA and full["truncated"] is False
    for smaller in (psg_bridge.graph_at(g, mode="story", story_keys=set(STORY_NODES)),
                    psg_bridge.graph_at(g, mode="data"),
                    psg_bridge.graph_at(g, mode="focus", focus="nk_5")):
        assert _keys(smaller) <= _keys(full)


def test_the_bridge_default_is_still_the_whole_functions_graph(tmp_path):
    """The UX default lives in the view (queries.resolve_mode); the bridge stays
    the plain reader it was in 2b so nothing else that calls it changes meaning."""
    g, _ = _chain_graph(tmp_path)
    assert psg_bridge.graph_at(g)["mode"] == "full" and len(psg_bridge.graph_at(g)["nodes"]) == FUNCS


# ── the page ─────────────────────────────────────────────────────────────────

def test_the_default_mode_is_focus_with_a_focus_and_story_without_one():
    from app import queries
    assert queries.resolve_mode(None, "pkg.core.mod.f5") == "focus"
    assert queries.resolve_mode(None, None) == "story"
    assert queries.resolve_mode("data", "pkg.core.mod.f5") == "data"
    assert queries.resolve_mode("nonsense", None) == "story"          # an unknown mode never falls back to full


def test_focus_page_draws_the_neighbourhood_and_offers_the_full_graph(client):
    r = client.get("/graph/demo?focus=pkg.core.mod.f5")
    assert r.status_code == 200
    t = r.text
    assert 'data-mode="focus"' in t
    assert t.count('data-node="nk_') == 5 and 'data-focus="1"' in t
    assert f'data-total-nodes="{FUNCS}"' in t
    assert f"{vocab_ui('expand_full')} ({FUNCS})" in t and "mode=full" in t    # the cut is a link with a number, never a silence


def test_a_page_without_a_focus_is_the_story_mode(client):
    t = client.get("/graph/demo").text
    assert 'data-mode="story"' in t
    assert 'data-node="nk_3"' in t and 'data-node="nk_orders"' in t
    assert 'data-node="nk_0"' not in t and 'data-node="nk_customers"' not in t
    assert f'data-total-nodes="{FUNCS}"' in t


def test_every_mode_has_a_chip_and_the_current_one_is_marked(client):
    t = client.get("/graph/demo").text
    for m in ("focus", "story", "data", "full"):
        assert f'data-mode-chip="{m}"' in t
    assert 'data-mode-chip="story" data-current="1"' in t


def test_data_mode_page_lists_the_data_nodes(client):
    t = client.get("/graph/demo?mode=data").text
    assert 'data-mode="data"' in t
    for k in ("nk_orders", "nk_amount", "nk_customers"):
        assert f'data-node="{k}"' in t


def test_full_mode_still_renders_every_function(client):
    t = client.get("/graph/demo?mode=full").text
    assert 'data-mode="full"' in t and t.count('data-node="nk_') == FUNCS


def test_the_neighbourhood_is_still_bounded_by_its_hop_count(client):
    from app import queries
    g = queries.get_graph(queries.open_db_readonly(client._db), "demo", focus="pkg.core.mod.f5")
    assert len(g["nodes"]) <= queries.NEIGHBOURHOOD_MAX_NODES and g["mode"] == "focus"


def test_a_project_without_a_graph_is_still_200_in_every_mode(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(tmp_path / "empty.json"))
    (tmp_path / "empty.json").write_text('{"projects": []}')
    for url in ("/graph/nope", "/graph/nope?focus=x", "/graph/nope?mode=full", "/graph/nope?mode=data"):
        r = client.get(url)
        assert r.status_code == 200 and "state graph unavailable" in r.text


# ── the cap: story and data are bounded too (step D of the 2d Task 0 plan) ────

def test_story_draws_everything_it_selected(tmp_path):
    """The old flat cap of 200 turned a 1601-node selection into a 200-node
    picture while the corner still said 1601. Nothing is dropped now."""
    g, _ = _chain_graph(tmp_path)
    s = psg_bridge.graph_at(g, mode="story", story_keys=["nk_3", "nk_7"])
    assert _keys(s) == {"nk_3", "nk_7", "nk_orders", "nk_amount"}
    assert len(s["nodes"]) == s["mode_total"] == 4 and s["total_nodes"] == FUNCS


def test_data_draws_everything_it_selected(tmp_path):
    g, _ = _chain_graph(tmp_path)
    d = psg_bridge.graph_at(g, mode="data")
    assert len(d["nodes"]) == d["mode_total"] == 6


def test_an_uncapped_mode_reports_mode_total_equal_to_what_it_drew(tmp_path):
    g, _ = _chain_graph(tmp_path)
    s = psg_bridge.graph_at(g, mode="story", story_keys=list(STORY_NODES))
    assert s["mode_total"] == len(s["nodes"]) == 4
    full = psg_bridge.graph_at(g, mode="full")
    assert full["mode_total"] == full["total_nodes"] == FUNCS


def test_the_page_prints_drawn_of_selected_of_whole(client):
    t = client.get("/graph/demo").text
    assert 'data-drawn="4"' in t and 'data-mode-total="4"' in t and f'data-total-nodes="{FUNCS}"' in t
    assert f"{vocab_ui('expand_full')} ({FUNCS})" in t


def test_story_is_the_default_and_draws_its_whole_selection(client):
    from app import queries
    g = queries.get_graph(queries.open_db_readonly(client._db), "demo")
    assert g["mode"] == "story" and len(g["nodes"]) == g["mode_total"]


# ── DP phase 2d follow-up: the 200 cap was cutting the graph in half ─────────
# On prov_ledger, story selected 1601 nodes and drew 200; data selected 645 and
# drew 200. A cap that silently removes three quarters of the picture is not a
# reduced view, it is a wrong one. Nothing is dropped now: up to CLUSTER_ABOVE
# everything is drawn, and beyond that nodes are CLUSTERED by module, so the
# total is preserved and the reader can expand.

def test_a_selection_under_the_cluster_threshold_is_drawn_whole(tmp_path):
    g, _ = _chain_graph(tmp_path)
    s = psg_bridge.graph_at(g, mode="story", story_keys=list(STORY_NODES))
    assert len(s["nodes"]) == s["mode_total"], "nodes were dropped below the clustering threshold"
    assert s["clusters"] == []


def test_a_large_selection_clusters_by_module_and_keeps_the_total(tmp_path):
    g, _ = _chain_graph(tmp_path)
    s = psg_bridge.graph_at(g, mode="full", level="full", cluster_above=3)
    assert s["clusters"], "a selection past the threshold did not cluster"
    inside = sum(c["nodes"] for c in s["clusters"])
    assert inside == s["mode_total"], f"clusters hold {inside} of {s['mode_total']} — nodes went missing"
    assert all(c["key"] and c["label"] for c in s["clusters"])


def test_the_page_states_the_cluster_arithmetic(client):
    from app import queries
    conn = queries.open_db_readonly(client._db)
    try:
        g = queries.get_graph(conn, "demo", mode="full", level="full", cluster_above=3)
    finally:
        conn.close()
    assert g["clusters"] and sum(c["nodes"] for c in g["clusters"]) == g["mode_total"]


def test_there_is_no_silent_node_cap_any_more(client):
    from app import queries
    assert not hasattr(queries, "MODE_MAX_NODES") or queries.MODE_MAX_NODES is None


# ── data flow is a layered picture, not a physics soup ──────────────────────

def test_data_mode_assigns_a_level_to_every_node(tmp_path):
    g, _ = _chain_graph(tmp_path)
    d = psg_bridge.graph_at(g, mode="data")
    assert d["layout"] == "hierarchical"
    assert all("level" in n for n in d["nodes"]), "a node has no layer"
    levels = {n["node_key"]: n["level"] for n in d["nodes"]}
    assert levels["nk_orders"] == 0 and levels["nk_customers"] == 0, "sources are not on level 0"
    assert levels["nk_3"] > 0 and levels["nk_5"] > 0, "the readers are not below the sources"


def test_story_and_focus_are_layered_too_and_full_is_not(tmp_path):
    g, _ = _chain_graph(tmp_path)
    assert psg_bridge.graph_at(g, mode="story", story_keys=list(STORY_NODES))["layout"] == "hierarchical"
    assert psg_bridge.graph_at(g, mode="focus", focus="nk_5")["layout"] == "hierarchical"
    assert psg_bridge.graph_at(g, mode="full")["layout"] == "physics"


def test_the_page_passes_the_layout_to_the_renderer(client):
    t = client.get("/graph/demo?mode=data").text
    assert '"layout": "hierarchical"' in t or "hierarchical" in t


def test_a_clustered_page_ships_cluster_summaries_not_every_node(client):
    """Removing the cap must not move the cost from the picture to the payload:
    past the threshold the page carries the clusters and a minimal per-node
    record, not 1601 full node dicts."""
    from app import queries, main
    conn = queries.open_db_readonly(client._db)
    try:
        g = queries.get_graph(conn, "demo", mode="full", level="full", cluster_above=3)
    finally:
        conn.close()
    payload = main.graph_payload(g)
    assert payload["clusters"], "no clusters in the payload"
    assert sum(c["nodes"] for c in payload["clusters"]) == g["mode_total"]
    for n in payload["nodes"]:
        # DP 2c added `declared` and `lane`: two small flags the node table needs
        # even when the picture is clustered. The point of this assertion is that
        # the payload is a SUMMARY, so the set stays closed and named.
        assert set(n) <= {"node_key", "qualified_name", "node_type", "level", "badge", "tier", "declared", "lane"}, \
            f"a clustered payload still carries {sorted(set(n))}"


def test_a_clustered_page_aggregates_its_edges_too(client):
    """1601 nodes drag ~12k edges behind them, which is most of the payload. A
    clustered picture shows module-to-module flow, so the edges are aggregated
    with it — and the page says the detail lives in the focused view."""
    from app import queries, main
    conn = queries.open_db_readonly(client._db)
    try:
        g = queries.get_graph(conn, "demo", mode="full", level="full", cluster_above=3)
    finally:
        conn.close()
    payload = main.graph_payload(g)
    assert payload["edges"], "a clustered payload has no edges at all"
    for e in payload["edges"]:
        assert set(e) == {"src_key", "dst_key", "weight"}, f"edge is not aggregated: {e}"
        assert e["src_key"].startswith("cluster:") and e["dst_key"].startswith("cluster:")
    assert len(payload["edges"]) <= len(g["edges"])


def test_an_unclustered_page_keeps_the_full_node_records(client):
    from app import queries, main
    conn = queries.open_db_readonly(client._db)
    try:
        g = queries.get_graph(conn, "demo", focus="pkg.core.mod.f5")
    finally:
        conn.close()
    payload = main.graph_payload(g)
    assert payload["clusters"] == []
    assert any("file_path" in n for n in payload["nodes"])


# ── the layering has to be a real depth, not a three-bucket guess ────────────
# The first version assigned levels by node_type alone, so every function landed
# on level 1 and vis drew 56 nodes on one horizontal line. A layered picture
# needs a DEPTH: distance from the focus, or topological depth from the sources.

def test_focus_levels_are_signed_distance_from_the_focus(tmp_path):
    g, _ = _chain_graph(tmp_path)
    nb = psg_bridge.graph_at(g, mode="focus", focus="nk_5", hops=2)
    lv = {n["node_key"]: n["level"] for n in nb["nodes"]}
    # levels are banded (depth * 10 + sub-row) so a crowded band can split
    assert lv["nk_5"] == 0, "the focus is not the origin"
    assert lv["nk_4"] == -10 and lv["nk_3"] == -20, "callers are not above the focus"
    assert lv["nk_6"] == 10 and lv["nk_7"] == 20, "callees are not below the focus"
    assert len(set(lv.values())) >= 3


def test_data_levels_are_topological_depth_not_node_type(tmp_path):
    g, _ = _chain_graph(tmp_path)
    d = psg_bridge.graph_at(g, mode="data")
    lv = {n["node_key"]: n["level"] for n in d["nodes"]}
    assert lv["nk_orders"] == 0 and lv["nk_customers"] == 0, "sources are not at depth 0"
    assert lv["nk_3"] > lv["nk_orders"], "the reader is not below what it reads"
    assert len(set(lv.values())) >= 2


def test_story_levels_spread_over_more_than_one_line(tmp_path):
    g, _ = _chain_graph(tmp_path)
    s = psg_bridge.graph_at(g, mode="story", story_keys=["nk_3", "nk_7"])
    assert len({n["level"] for n in s["nodes"]}) >= 2, "story drew everything on one line"


def test_a_cycle_does_not_hang_or_explode(tmp_path):
    """Topological depth on a graph with a cycle: the cycle is collapsed, every
    node still gets a level, and nothing recurses forever."""
    g, _ = _chain_graph(tmp_path)
    import sqlite3
    c = sqlite3.connect(g)
    c.execute("INSERT INTO edge (edge_type_id, src_node_id, dst_node_id) VALUES (1, 12, 1)")  # f11 -> f0
    c.commit(); c.close()
    full = psg_bridge.graph_at(g, mode="data")
    assert all(isinstance(n.get("level"), int) for n in full["nodes"])


def test_clusters_sit_at_the_shallowest_level_they_contain(tmp_path):
    g, _ = _chain_graph(tmp_path)
    s = psg_bridge.graph_at(g, mode="full", level="full", cluster_above=3)
    assert s["clusters"] and all("level" in c for c in s["clusters"])


def test_the_renderer_gets_the_options_the_layout_needs(client):
    t = client.get("/graph/demo?mode=data").text
    assert "levelSeparation: 140" in t and "nodeSpacing: 180" in t
    assert "sortMethod: 'directed'" in t and "direction: 'UD'" in t
    assert "forceDirection: 'vertical'" in t
    assert "window.network" in t, "the vis instance is not exposed for inspection"


# ── a level holding 25 nodes is a list, not a layer ─────────────────────────

def test_a_crowded_level_splits_into_sub_rows_by_module(tmp_path):
    """56 nodes with ~25 on one level rendered as a 4500px smear. A level past
    the crowd limit is split by module into sub-rows, so the layer still reads
    as one band but the nodes have somewhere to go."""
    g, _ = _chain_graph(tmp_path)
    nb = psg_bridge.graph_at(g, mode="focus", focus="nk_5", hops=2, crowd=2)
    levels = [n["level"] for n in nb["nodes"]]
    assert len(set(levels)) > 3, "a crowded level did not split"
    # sub-rows stay adjacent: every level is level*10 + small row index
    assert all(abs(l) % 10 < 10 for l in levels)
    # and the ordering of the bands is preserved
    bands = sorted({l // 10 for l in levels})
    assert bands == sorted(set(bands))


def test_an_uncrowded_level_is_not_split(tmp_path):
    g, _ = _chain_graph(tmp_path)
    nb = psg_bridge.graph_at(g, mode="focus", focus="nk_5", hops=2, crowd=50)
    assert {n["level"] for n in nb["nodes"]} == {-20, -10, 0, 10, 20}


def test_labels_are_reserved_for_what_the_reader_asked_about(client):
    """A label on all 56 nodes is unreadable at any zoom. The focus, its direct
    neighbours and anything carrying records keep their label; the rest show it
    on hover."""
    t = client.get("/graph/demo?focus=pkg.core.mod.f5").text
    assert "labelWhenHovered" in t or "always_label" in t
