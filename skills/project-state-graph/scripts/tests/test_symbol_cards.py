"""Tests for analyzer.cards.build_symbol_cards (deep-layer retrieval unit)."""
import json

from analyzer import store, walker, py_ast, dataflow, dataflow_types, cards


def _analyze(tmp_path, src: str):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text(src)
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    return conn


SRC = (
    "def producer() -> list[int]:\n    return [1]\n\n"
    "def consumer(x):\n    return x\n\n"
    "def main():\n    data = producer()\n    consumer(data)\n"
)


def test_symbol_card_table_created(tmp_path):
    conn = _analyze(tmp_path, SRC)
    cards.build_symbol_cards(conn)
    n = conn.execute("SELECT COUNT(*) FROM symbol_card").fetchone()[0]
    assert n > 0


def test_retrievable_by_qualified_name(tmp_path):
    conn = _analyze(tmp_path, SRC)
    cards.build_symbol_cards(conn)
    row = conn.execute(
        "SELECT card_json FROM symbol_card WHERE qualified_name LIKE '%producer'"
    ).fetchone()
    assert row is not None
    rec = json.loads(row[0])
    assert rec["name"] == "producer"
    assert rec["output_type"] == "list[int]"
    assert "consumer" in rec["consistency"]["output_consumers"]


def test_record_is_self_contained(tmp_path):
    conn = _analyze(tmp_path, SRC)
    recs = cards.build_symbol_cards(conn)
    sample = next(iter(recs.values()))
    for key in ("name", "qualified_name", "kind", "file_path",
                "line_start", "line_end", "output_type", "consistency"):
        assert key in sample
    # json round-trips (compact + serializable)
    assert json.loads(json.dumps(sample)) == sample


# ── phase 2 Task 10: symbol_card carries node_key + recent history ───────────

def _history_run(tmp_path, src, plan_id):
    """Rebuild into the same DB the way cli.run does, history layer included."""
    from analyzer import history, store as _store
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True, exist_ok=True)
    (repo / "pkg" / "m.py").write_text(src)
    conn = _store.init_db(str(tmp_path / "demo-state-graph.db"))
    run_id = _store.start_run(conn, project_name="demo", commit_sha="c", plan_id=plan_id)
    _store.reset_graph(conn)
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    cards.build_symbol_cards(conn)
    history.snapshot_run(conn, str(repo), run_id)
    history.resolve(conn, run_id)
    cards.attach_history(conn)
    _store.stamp_run(conn, run_id)
    _store.finish_run(conn, run_id)
    return conn


def _card(conn, qn):
    return json.loads(conn.execute("SELECT card_json FROM symbol_card WHERE qualified_name=?", (qn,)).fetchone()[0])


def test_attach_history_adds_node_key_and_recent_events(tmp_path):
    conn = _history_run(tmp_path, SRC, "P1"); conn.close()
    conn = _history_run(tmp_path, SRC.replace("return [1]", "return [1, 2]"), "P2")
    card = _card(conn, "pkg.m.producer")
    key = conn.execute("SELECT node_key FROM node WHERE qualified_name='pkg.m.producer'").fetchone()[0]
    assert key and card["node_key"] == key
    kinds = [h["event_type"] for h in card["history"]]
    assert kinds == ["node_added", "node_matched", "node_changed"]          # oldest first
    assert [h["plan_id"] for h in card["history"]] == ["P1", "P2", "P2"]
    assert {"run_id", "event_type", "tier", "plan_id", "step_id", "trigger", "commit_sha", "created_at", "payload"} <= set(card["history"][0])
    # the untouched consumer only matched
    assert [h["event_type"] for h in _card(conn, "pkg.m.consumer")["history"]] == ["node_added", "node_matched"]
    # the rest of the card is untouched
    assert card["qualified_name"] == "pkg.m.producer" and "consistency" in card


def test_attach_history_caps_at_ten(tmp_path):
    src = SRC
    for i in range(12):
        conn = _history_run(tmp_path, src.replace("return [1]", f"return [{i}]"), f"P{i}")
        if i < 11:
            conn.close()
    hist = _card(conn, "pkg.m.producer")["history"]
    assert len(hist) == 10 and hist[-1]["plan_id"] == "P11" and hist[-1]["event_type"] == "node_changed"
