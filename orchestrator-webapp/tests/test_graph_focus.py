"""DP phase 2d (Task 0, half of FL-076): the Graph view stops drawing the whole
project on every load.

User feedback on the 2b screenshots, 2026-09-16: 记全是对的，画全是错的 — recording
every node is right, DRAWING every node is wrong. So the view has four modes and
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
    assert f"展开全图（{FUNCS} 个节点）" in t and "mode=full" in t    # the cut is a link with a number, never a silence


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


def test_the_neighbourhood_is_capped_even_for_a_hub(client):
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

def test_story_is_capped_and_says_how_much_it_drew(tmp_path):
    """prov_ledger has 1163 nodes with a story: `story` without a cap is 2 MB.
    The cap keeps the most-storied seeds (the caller's order) and the page says
    `drawn / selected / whole` — the crop is a number, not a silence."""
    g, _ = _chain_graph(tmp_path)
    ordered = ["nk_3", "nk_7"]                       # most-badged first, as queries.get_graph sorts them
    s = psg_bridge.graph_at(g, mode="story", story_keys=ordered, max_nodes=1)
    assert _keys(s) == {"nk_3"} and s["truncated"] is True
    assert s["mode_total"] == 4 and s["total_nodes"] == FUNCS       # 2 seeds + 2 data nodes were selected
    assert len(s["nodes"]) <= 1


def test_data_is_capped_the_same_way(tmp_path):
    g, _ = _chain_graph(tmp_path)
    d = psg_bridge.graph_at(g, mode="data", max_nodes=2)
    assert len(d["nodes"]) == 2 and d["truncated"] is True and d["mode_total"] == 6
    assert {n["node_type"] for n in d["nodes"]} <= {"sql_table", "column"}   # data nodes first, functions are the tail


def test_an_uncapped_mode_reports_mode_total_equal_to_what_it_drew(tmp_path):
    g, _ = _chain_graph(tmp_path)
    s = psg_bridge.graph_at(g, mode="story", story_keys=list(STORY_NODES))
    assert s["mode_total"] == len(s["nodes"]) == 4
    full = psg_bridge.graph_at(g, mode="full")
    assert full["mode_total"] == full["total_nodes"] == FUNCS


def test_the_page_prints_drawn_of_selected_of_whole(client):
    t = client.get("/graph/demo").text
    assert 'data-drawn="4"' in t and 'data-mode-total="4"' in t and f'data-total-nodes="{FUNCS}"' in t
    assert f"展开全图（{FUNCS} 个节点）" in t


def test_the_cap_the_page_uses_is_the_one_the_module_publishes(client):
    from app import queries
    assert queries.MODE_MAX_NODES <= queries.NEIGHBOURHOOD_MAX_NODES
    g = queries.get_graph(queries.open_db_readonly(client._db), "demo")
    assert len(g["nodes"]) <= queries.MODE_MAX_NODES and g["mode"] == "story"
