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
