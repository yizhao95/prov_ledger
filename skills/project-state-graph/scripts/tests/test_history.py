"""analyzer.history — snapshot, three-layer match, append-only events (spec §2.4)."""
import json
from pathlib import Path

import pytest

from analyzer import (data_model, dataflow, dataflow_types, history, py_ast, sql_refs,
                      store, walker)


def _full_run(conn, repo: Path, files: dict, *, plan_id=None) -> int:
    """Write `files`, rebuild the graph into `conn` and run the history layer
    exactly the way cli.run will: snapshot + resolve BEFORE stamp_run."""
    for rel, src in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    run_id = store.start_run(conn, project_name="demo", commit_sha="c", plan_id=plan_id)
    store.reset_graph(conn)
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    data_model.analyze(conn, str(repo), fm)
    sql_refs.analyze(conn, str(repo), fm)
    history.snapshot_run(conn, str(repo), run_id)
    history.resolve(conn, run_id)
    store.stamp_run(conn, run_id)
    store.finish_run(conn, run_id)
    return run_id


def _events(conn, run_id):
    return [(r[0], r[1], json.loads(r[2]), r[3]) for r in conn.execute(
        "SELECT event_type, node_key, payload_json, tier FROM node_event WHERE run_id=? ORDER BY seq", (run_id,))]


def _key(conn, run_id, qn):
    row = conn.execute("SELECT node_key FROM node_snapshot WHERE run_id=? AND qualified_name=?", (run_id, qn)).fetchone()
    return row[0] if row else None


@pytest.fixture
def conn(tmp_path):
    c = store.init_db(str(tmp_path / "demo-state-graph.db"))
    yield c
    c.close()


BASE = {"pkg/__init__.py": "", "pkg/m.py": "def load(path):\n    x = path + 1\n    return x\n\n"
                                            "def clean(df):\n    df = df[df.q > 0]\n    return df\n\n"
                                            "def main():\n    return clean(load('p'))\n"}


def test_first_run_all_added_and_keys_backfilled(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", BASE)
    ev = _events(conn, r1)
    assert ev and {e[0] for e in ev} == {"node_added"}
    assert all(e[3] == "observed" for e in ev)
    keys = [k for (k,) in conn.execute("SELECT node_key FROM node_snapshot WHERE run_id=?", (r1,))]
    assert keys and all(k.startswith("nk_") for k in keys)
    unkeyed = conn.execute(
        "SELECT COUNT(*) FROM node n JOIN node_type t ON n.node_type_id=t.id "
        "WHERE t.name IN ('function','method','class') AND (n.node_key IS NULL OR n.node_key='')").fetchone()[0]
    assert unkeyed == 0


def test_identical_second_run_only_matches(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", BASE)
    r2 = _full_run(conn, tmp_path / "r2", BASE)
    ev = _events(conn, r2)
    assert {e[0] for e in ev} == {"node_matched"}
    assert all(e[2]["via"] == "qualname" and e[2]["prev_run_id"] == r1 for e in ev)
    cols = "node_key, node_type, qualified_name, file_path, line_start, line_end, struct_sig, dataflow_sig, dataflow_trivial"
    s1 = conn.execute(f"SELECT {cols} FROM node_snapshot WHERE run_id=? ORDER BY node_type, qualified_name", (r1,)).fetchall()
    s2 = conn.execute(f"SELECT {cols} FROM node_snapshot WHERE run_id=? ORDER BY node_type, qualified_name", (r2,)).fetchall()
    assert s1 == s2
    assert _key(conn, r1, "pkg.m.load") == _key(conn, r2, "pkg.m.load")


def test_rename_is_matched_via_struct_sig(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", BASE)
    files = dict(BASE); files["pkg/m.py"] = BASE["pkg/m.py"].replace("def load(path)", "def fetch(path)").replace("load('p')", "fetch('p')")
    r2 = _full_run(conn, tmp_path / "r2", files)
    assert _key(conn, r2, "pkg.m.fetch") == _key(conn, r1, "pkg.m.load")
    ev = {(e[0], e[1]): e[2] for e in _events(conn, r2)}
    k = _key(conn, r1, "pkg.m.load")
    assert ev[("node_matched", k)]["via"] == "struct_sig"
    assert ev[("node_renamed", k)] == {"from": "pkg.m.load", "to": "pkg.m.fetch"}
    assert not [e for e in _events(conn, r2) if e[0] == "node_removed"]
    # main's body changed (call target renamed) -> node_changed on main, not on fetch
    assert ("node_changed", _key(conn, r1, "pkg.m.main")) in ev
    assert ("node_changed", k) not in ev


def test_move_file_is_matched_and_moved(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", BASE)
    files = {"pkg/__init__.py": "", "pkg/io.py": BASE["pkg/m.py"]}
    r2 = _full_run(conn, tmp_path / "r2", files)
    k = _key(conn, r1, "pkg.m.clean")
    assert _key(conn, r2, "pkg.io.clean") == k
    ev = {(e[0], e[1]): e[2] for e in _events(conn, r2)}
    assert ev[("node_moved", k)] == {"from": "pkg/m.py", "to": "pkg/io.py"}
    assert ev[("node_renamed", k)]["to"] == "pkg.io.clean"


def test_statement_change_is_node_changed(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", BASE)
    files = dict(BASE); files["pkg/m.py"] = BASE["pkg/m.py"].replace("df.q > 0", "df.q > 1")
    r2 = _full_run(conn, tmp_path / "r2", files)
    k = _key(conn, r1, "pkg.m.clean")
    ev = {(e[0], e[1]): e[2] for e in _events(conn, r2)}
    assert "struct_sig" in ev[("node_changed", k)]["changed"]
    assert ev[("node_changed", k)]["struct_sig"]["from"] != ev[("node_changed", k)]["struct_sig"]["to"]
    assert ("node_changed", _key(conn, r1, "pkg.m.load")) not in ev


def test_delete_is_node_removed(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", BASE)
    files = dict(BASE); files["pkg/m.py"] = "def load(path):\n    x = path + 1\n    return x\n\ndef main():\n    return load('p')\n"
    r2 = _full_run(conn, tmp_path / "r2", files)
    k = _key(conn, r1, "pkg.m.clean")
    ev = {(e[0], e[1]): e[2] for e in _events(conn, r2)}
    assert ev[("node_removed", k)]["qualified_name"] == "pkg.m.clean"
    assert ev[("node_removed", k)]["last_seen_run"] == r1


TWINS = {"pkg/__init__.py": "", "pkg/m.py": "def norm_a(x):\n    return x + 1\n\ndef norm_b(x):\n    return x + 1\n"}
TWINS_RENAMED = {"pkg/__init__.py": "", "pkg/m.py": "def scale_a(x):\n    return x + 1\n\ndef scale_b(x):\n    return x + 1\n"}


def test_two_identical_renamed_functions_are_ambiguous_not_removed(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", TWINS)
    r2 = _full_run(conn, tmp_path / "r2", TWINS_RENAMED)
    ev = _events(conn, r2)
    kinds = [e[0] for e in ev]
    assert "identity_ambiguous" in kinds and "node_removed" not in kinds and "node_matched" not in kinds
    amb = next(e for e in ev if e[0] == "identity_ambiguous")
    assert amb[1] is None and amb[2]["layer"] == "struct_sig"
    assert sorted(amb[2]["prev"]) == ["pkg.m.norm_a", "pkg.m.norm_b"]
    assert sorted(amb[2]["cur"]) == ["pkg.m.scale_a", "pkg.m.scale_b"]
    # never silently linked: the new rows get provisional keys distinct from the old ones
    old = {_key(conn, r1, "pkg.m.norm_a"), _key(conn, r1, "pkg.m.norm_b")}
    new = {_key(conn, r2, "pkg.m.scale_a"), _key(conn, r2, "pkg.m.scale_b")}
    assert old.isdisjoint(new) and "" not in new


def test_arbitration_writes_asserted_event_with_evidence(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", TWINS)
    k_a = _key(conn, r1, "pkg.m.norm_a")
    for rel, src in TWINS_RENAMED.items():
        (tmp_path / "r2" / rel).parent.mkdir(parents=True, exist_ok=True); (tmp_path / "r2" / rel).write_text(src)
    r2 = store.start_run(conn, project_name="demo")
    store.reset_graph(conn)
    fm = walker.walk(conn, str(tmp_path / "r2")); py_ast.analyze(conn, str(tmp_path / "r2"), fm)
    history.snapshot_run(conn, str(tmp_path / "r2"), r2)

    def arbitrate(ambs):
        assert len(ambs) == 1
        return [history.Assertion("pkg.m.scale_a", k_a, "git log -p shows norm_a renamed to scale_a", "test-stub")]
    counts = history.resolve(conn, r2, arbitrate=arbitrate)
    assert counts["identity_ambiguous"] == 1 and counts["identity_asserted"] == 1
    ev = _events(conn, r2)
    assert ev[-1][0] == "identity_asserted" and ev[-1][3] == "asserted" and ev[-1][1] == k_a
    assert ev[-1][2]["cur"] == "pkg.m.scale_a" and ev[-1][2]["evidence"]


def test_arbitration_without_evidence_is_rejected(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", TWINS)
    k_a = _key(conn, r1, "pkg.m.norm_a")
    for rel, src in TWINS_RENAMED.items():
        (tmp_path / "r2" / rel).parent.mkdir(parents=True, exist_ok=True); (tmp_path / "r2" / rel).write_text(src)
    r2 = store.start_run(conn, project_name="demo")
    store.reset_graph(conn)
    fm = walker.walk(conn, str(tmp_path / "r2")); py_ast.analyze(conn, str(tmp_path / "r2"), fm)
    history.snapshot_run(conn, str(tmp_path / "r2"), r2)
    with pytest.raises(ValueError):
        history.resolve(conn, r2, arbitrate=lambda a: [history.Assertion("pkg.m.scale_a", k_a, "  ", "stub")])


def test_event_order_is_fixed(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", BASE)
    files = dict(BASE)
    files["pkg/m.py"] = ("def fetch(path):\n    x = path + 1\n    return x\n\n"        # load renamed
                         "def main():\n    return fetch('p')\n\n"                        # clean removed, main changed
                         "def extra():\n    return 42\n")                                 # added
    r2 = _full_run(conn, tmp_path / "r2", files)
    kinds = [e[0] for e in _events(conn, r2)]
    order = [history.EVENT_ORDER.index(k) for k in kinds]
    assert order == sorted(order)
    assert {"node_matched", "node_renamed", "node_changed", "node_added", "node_removed"} <= set(kinds)


def test_match_is_pure_over_rows():
    def row(i, qn, sig, ntype="function", df=None, trivial=True, key=""):
        return history.Row(i, None, key, ntype, qn, "m.py", 1, 2, sig, df, trivial)
    prev = [row(1, "m.a", "s1", key="nk_a"), row(2, "m.b", "s2", key="nk_b"), row(3, "m.c", "s3", key="nk_c")]
    cur = [row(11, "m.a", "s1x"), row(12, "m.b2", "s2"), row(13, "m.d", "s9")]
    out = history.match(prev, cur)
    assert [(p.prev.qualified_name, p.cur.qualified_name, p.via) for p in out.pairs] == \
        [("m.a", "m.a", "qualname"), ("m.b", "m.b2", "struct_sig")]
    assert [r.qualified_name for r in out.removed] == ["m.c"]
    assert [r.qualified_name for r in out.added] == ["m.d"]
    assert out.ambiguous == [] and prev[0].node_key == "nk_a"   # inputs untouched
    # dataflow layer: same non-trivial dataflow_sig, 1:1
    p2 = [row(1, "m.a", "s1", df="d1", trivial=False, key="nk_a")]
    c2 = [row(2, "m.z", "s7", df="d1", trivial=False)]
    assert history.match(p2, c2).pairs[0].via == "dataflow_sig"
    # trivial dataflow never pairs
    assert history.match([row(1, "m.a", "s1", df="d1", key="nk_a")], [row(2, "m.z", "s7", df="d1")]).pairs == []


# ── P2-A: column / dataframe identity via owner inheritance (E0, D1) ─────────

COL1 = {"pkg/__init__.py": "",
        "pkg/m.py": 'import pandas as pd\n\ndef load():\n    df = pd.read_csv("a.csv")\n    df["amount"] = df["qty"] * 2\n    return df\n'}
COL2 = {"pkg/__init__.py": "", "pkg/m.py": COL1["pkg/m.py"].replace("def load()", "def load_orders()")}


def _col_key(conn, run_id, suffix):
    row = conn.execute("SELECT node_key, qualified_name FROM node_snapshot WHERE run_id=? AND node_type='column' "
                       "AND qualified_name LIKE ?", (run_id, f"%.{suffix}")).fetchone()
    return row


def test_e0_1_column_identity_survives_owner_rename(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", COL1)
    r2 = _full_run(conn, tmp_path / "r2", COL2)
    k1, qn1 = _col_key(conn, r1, "amount")
    k2, qn2 = _col_key(conn, r2, "amount")
    assert qn1 == "pkg.m.load:df.amount" and qn2 == "pkg.m.load_orders:df.amount"
    assert k1 == k2
    via = [json.loads(p)["via"] for (p,) in conn.execute(
        "SELECT payload_json FROM node_event WHERE run_id=? AND event_type='node_matched' AND node_key=?", (r2, k1))]
    assert via == ["owner"]
    assert not [e for e in _events(conn, r2) if e[0] in ("node_removed", "node_added")]


def test_e0_2_history_of_column_is_one_query(conn, tmp_path):
    _full_run(conn, tmp_path / "r1", COL1, plan_id="P1")
    _full_run(conn, tmp_path / "r2", COL2, plan_id="P2")
    ev = history.events_of(conn, "pkg.m.load_orders:df.amount")
    # the column's composite qualified name follows its owner's rename, so its
    # own history records the rename too — one query, every event
    assert [e["event_type"] for e in ev] == ["node_added", "node_matched", "node_renamed"]
    assert [e["plan_id"] for e in ev] == ["P1", "P2", "P2"]
    assert ev[2]["payload"] == {"from": "pkg.m.load:df.amount", "to": "pkg.m.load_orders:df.amount"}
    assert history.events_of(conn, ev[0]["payload"] and conn.execute(
        "SELECT node_key FROM node_event WHERE id=?", (ev[0]["event_id"],)).fetchone()[0]) == ev


def test_dataframe_identity_survives_owner_rename(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", COL1)
    r2 = _full_run(conn, tmp_path / "r2", COL2)
    assert _key(conn, r1, "pkg.m.load:df") == _key(conn, r2, "pkg.m.load_orders:df")
    ev = {(e[0], e[1]): e[2] for e in _events(conn, r2)}
    assert ev[("node_matched", _key(conn, r1, "pkg.m.load:df"))]["via"] == "owner"
    assert ev[("node_renamed", _key(conn, r1, "pkg.m.load:df"))]["to"] == "pkg.m.load_orders:df"


def test_column_removed_when_owner_removed(conn, tmp_path):
    r1 = _full_run(conn, tmp_path / "r1", COL1)
    r2 = _full_run(conn, tmp_path / "r2", {"pkg/__init__.py": "", "pkg/m.py": "def other():\n    return 1\n"})
    k1, _ = _col_key(conn, r1, "amount")
    ev = {(e[0], e[1]): e[2] for e in _events(conn, r2)}
    assert ev[("node_removed", k1)]["qualified_name"] == "pkg.m.load:df.amount"
    assert ("node_removed", _key(conn, r1, "pkg.m.load:df")) in ev
    assert ("node_removed", _key(conn, r1, "pkg.m.load")) in ev


def test_match_owner_layer_is_pure():
    def row(i, qn, ntype, key="", sig=None, owner=None, name=None):
        return history.Row(i, None, key, ntype, qn, "m.py", 1, 2, sig, None, True, owner_qn=owner, name=name)
    prev = [row(1, "m.f", "function", key="nk_f", sig="s1"),
            row(2, "m.f:df", "dataframe", key="nk_df", owner="m.f", name="df"),
            row(3, "m.f:df.amount", "column", key="nk_c", owner="m.f:df", name="amount")]
    cur = [row(11, "m.g", "function", sig="s1"),
           row(12, "m.g:df", "dataframe", owner="m.g", name="df"),
           row(13, "m.g:df.amount", "column", owner="m.g:df", name="amount"),
           row(14, "m.g:df.qty", "column", owner="m.g:df", name="qty")]
    out = history.match(prev, cur)
    assert {(p.prev.qualified_name, p.cur.qualified_name, p.via) for p in out.pairs} == {
        ("m.f", "m.g", "struct_sig"), ("m.f:df", "m.g:df", "owner"), ("m.f:df.amount", "m.g:df.amount", "owner")}
    assert [r.qualified_name for r in out.added] == ["m.g:df.qty"] and out.removed == [] and out.ambiguous == []
