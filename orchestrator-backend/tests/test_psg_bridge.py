"""orchestrator.psg_bridge — read-only window into a project's state graph."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from orchestrator import psg_bridge

sys.path.insert(0, str(Path(__file__).parent))     # `tests` is the repo-root package; import the sibling directly
import _psg_schema as ps  # noqa: E402

STORE_PY = Path(__file__).resolve().parents[2] / "skills" / "project-state-graph" / "scripts" / "analyzer" / "store.py"


def test_psg_schema_matches_analyzer():
    """The DDL the bridge is tested against must be the analyzer's, verbatim."""
    assert ps.HISTORY_SCHEMA in STORE_PY.read_text()


@pytest.fixture
def psg(tmp_path):
    """run 1 (plan P0): nk_a, nk_b, nk_c added. run 2 (plan P1): nk_a struct change,
    nk_b dataflow-only change (NOT a reason slot), nk_d added, nk_c removed, nk_x matched."""
    path = tmp_path / "demo-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0", step_id="P0-A")
    for key, qn in (("nk_a", "pkg.m.load"), ("nk_b", "pkg.m.clean"), ("nk_c", "pkg.m.gone"), ("nk_x", "pkg.m.keep")):
        ps.add_snapshot(c, 1, key, qn)
        ps.add_event(c, 1, len(key), "node_added", key, json.dumps({"qualified_name": qn}))
    ps.add_run(c, 2, plan_id="P1", step_id="P1-REVIEW.1")
    ps.add_snapshot(c, 2, "nk_a", "pkg.m.load_orders", struct_sig="s2")      # renamed + changed
    ps.add_snapshot(c, 2, "nk_b", "pkg.m.clean")
    ps.add_snapshot(c, 2, "nk_x", "pkg.m.keep")
    ps.add_snapshot(c, 2, "nk_d", "pkg.m.load_orders:df.amount", ntype="column")
    ps.add_event(c, 2, 1, "node_matched", "nk_a", '{"via": "struct_sig"}')
    ps.add_event(c, 2, 2, "node_matched", "nk_b", '{"via": "qualname"}')
    ps.add_event(c, 2, 3, "node_matched", "nk_x", '{"via": "qualname"}')
    ps.add_event(c, 2, 4, "node_renamed", "nk_a", '{"from": "pkg.m.load", "to": "pkg.m.load_orders"}')
    ps.add_event(c, 2, 5, "node_changed", "nk_a", '{"changed": ["struct_sig", "dataflow_sig"]}')
    ps.add_event(c, 2, 6, "node_changed", "nk_b", '{"changed": ["dataflow_sig"]}')
    ps.add_event(c, 2, 7, "node_added", "nk_d", '{"qualified_name": "pkg.m.load_orders:df.amount", "node_type": "column"}')
    ps.add_event(c, 2, 8, "node_removed", "nk_c", '{"qualified_name": "pkg.m.gone"}')
    ps.add_event(c, 2, 9, "identity_ambiguous", None, '{"layer": "struct_sig"}')
    c.commit(); c.close()
    return str(path)


def test_changed_node_keys_by_plan(psg):
    got = psg_bridge.changed_node_keys(psg, "P1")
    assert [(g["node_key"], g["qualified_name"], g["node_type"], sorted(g["event_types"]), g["run_id"]) for g in got] == [
        ("nk_a", "pkg.m.load_orders", "function", ["node_changed"], 2),
        ("nk_c", "pkg.m.gone", "function", ["node_removed"], 2),
        ("nk_d", "pkg.m.load_orders:df.amount", "column", ["node_added"], 2),
    ]
    assert psg_bridge.changed_node_keys(psg, "P0") and all(g["event_types"] == ["node_added"] for g in psg_bridge.changed_node_keys(psg, "P0"))
    assert psg_bridge.changed_node_keys(psg, "P9") == []


def test_bridge_never_writes(psg):
    conn = psg_bridge.open_ro(psg)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO node_event (run_id, seq, event_type, tier, payload_json, created_at) VALUES (1, 99, 'x', 'observed', '{}', 'now')")
    conn.close()


def test_missing_db_degrades_to_none(tmp_path):
    reg = tmp_path / "projects.json"
    assert psg_bridge.db_path_for("nope", registry_path=str(reg)) is None
    reg.write_text(json.dumps({"projects": [{"name": "demo", "db_path": str(tmp_path / "demo.db")}]}))
    assert psg_bridge.db_path_for("demo", registry_path=str(reg)) == str(tmp_path / "demo.db")
    assert psg_bridge.db_path_for("other", registry_path=str(reg)) is None
    absent = str(tmp_path / "absent.db")
    assert psg_bridge.changed_node_keys(absent, "P1") == []
    assert psg_bridge.node_key_of(absent, "pkg.m.load") is None
    assert psg_bridge.events_of(absent, "nk_a") == []
    assert psg_bridge.latest_run_id(absent) is None


def test_pre_history_graph_degrades(tmp_path):
    """A graph built before phase 2 has no node_snapshot/node_event tables."""
    path = tmp_path / "old.db"
    c = sqlite3.connect(str(path)); c.executescript(ps.BASE_SCHEMA); c.commit(); c.close()
    assert psg_bridge.changed_node_keys(str(path), "P1") == []
    assert psg_bridge.node_key_of(str(path), "x") is None
    assert psg_bridge.events_of(str(path), "nk_a") == []


def test_node_key_of_latest_snapshot(psg):
    assert psg_bridge.node_key_of(psg, "pkg.m.load_orders") == "nk_a"
    assert psg_bridge.node_key_of(psg, "pkg.m.load") == "nk_a"            # older name still resolves
    assert psg_bridge.node_key_of(psg, "pkg.m.load_orders:df.amount") == "nk_d"
    assert psg_bridge.node_key_of(psg, "pkg.m.nope") is None


def test_events_of_carries_attribution(psg):
    ev = psg_bridge.events_of(psg, "nk_a")
    assert [e["event_type"] for e in ev] == ["node_added", "node_matched", "node_renamed", "node_changed"]
    assert [e["plan_id"] for e in ev] == ["P0", "P1", "P1", "P1"]
    assert ev[1]["step_id"] == "P1-REVIEW.1" and ev[1]["commit_sha"] == "c0ffee" and ev[1]["trigger"] == "review"
    assert ev[3]["payload"] == {"changed": ["struct_sig", "dataflow_sig"]}
    assert {"event_id", "run_id", "seq", "tier", "created_at"} <= set(ev[0])


def test_latest_run_id(psg):
    assert psg_bridge.latest_run_id(psg) == 2
    assert psg_bridge.latest_run_id(psg, plan_id="P0") == 1
    assert psg_bridge.latest_run_id(psg, plan_id="P9") is None


# ── Phase 8 Task 4: the consistency card (space) and a node's constraints ──
def test_card_of_returns_callers_and_consumers_or_empty(psg):
    c = sqlite3.connect(psg)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
        INSERT INTO node_type (id, name) VALUES (1, 'function');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (7, 1, 'load_orders', 'pkg.m.load_orders', 'pkg/m.py', 2, 'nk_a');
        INSERT INTO consistency_card (symbol_id, card_json) VALUES (7, '{"callers": ["pkg.m.main"], "callees": ["pd.read_csv"], "output_consumers": ["pkg.m.clean"], "reads": ["orders"], "writes": [], "dtype_map": {"return": "DataFrame"}}');
    """)
    c.commit(); c.close()
    card = psg_bridge.card_of(psg, "pkg.m.load_orders")
    assert card["callers"] == ["pkg.m.main"] and card["output_consumers"] == ["pkg.m.clean"] and card["reads"] == ["orders"]
    assert psg_bridge.card_of(psg, "pkg.m.nope") == {} and psg_bridge.card_of(None, "x") == {}
    assert psg_bridge.card_of(str(Path(psg).parent / "missing.db"), "x") == {}
