"""RED tests for analyzer.cards.build_consistency_cards."""
import json

import pytest

from analyzer import store, walker, py_ast, dataflow, dataflow_types, sql_refs, pipeline, cards

from analyzer import data_model, profiles


def _analyze(tmp_path, files: dict):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    for rel, src in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    data_model.analyze(conn, str(repo), fm)
    sql_refs.analyze(conn, str(repo), fm)
    pipeline.analyze(conn, str(repo), fm)
    profiles.analyze(conn, str(repo), fm)
    return conn


def _card_by_name(conn, name):
    rows = conn.execute(
        """SELECT c.card_json
           FROM consistency_card c JOIN node n ON c.symbol_id = n.id
           WHERE n.name = ?""",
        (name,),
    ).fetchall()
    assert rows, f"no consistency_card for {name}"
    return json.loads(rows[0][0])


def test_table_created_and_populated(tmp_path):
    conn = _analyze(tmp_path, {"pkg/m.py": "def a():\n    return 1\n\ndef b():\n    return a()\n"})
    cards.build_consistency_cards(conn)
    n = conn.execute("SELECT COUNT(*) FROM consistency_card").fetchone()[0]
    assert n > 0


def test_callers_and_output_consumers(tmp_path):
    src = (
        "def parse_oos_rows():\n    return [1, 2]\n\n"
        "def dedupe(x):\n    return x\n\n"
        "def main():\n    rows = parse_oos_rows()\n    dedupe(rows)\n"
    )
    conn = _analyze(tmp_path, {"pkg/oos.py": src})
    cards.build_consistency_cards(conn)
    card = _card_by_name(conn, "parse_oos_rows")
    # main calls parse_oos_rows -> caller
    assert "main" in card["callers"]
    # dedupe consumes parse_oos_rows output -> output_consumer
    assert "dedupe" in card["output_consumers"]


def test_reads_and_writes_sql(tmp_path):
    src = (
        "def fetch():\n"
        "    q = \"SELECT * FROM project.dataset.events\"\n"
        "    return q\n"
    )
    conn = _analyze(tmp_path, {"pkg/q.py": src})
    cards.build_consistency_cards(conn)
    card = _card_by_name(conn, "fetch")
    assert "reads" in card and isinstance(card["reads"], list)


def test_pipeline_membership(tmp_path):
    src = (
        "def step_one():\n    return 1\n\n"
        "def step_two():\n    return 2\n\n"
        "def step_three():\n    return 3\n\n"
        "def orchestrate():\n    step_one()\n    step_two()\n    step_three()\n"
    )
    conn = _analyze(tmp_path, {"pkg/pipe.py": src})
    cards.build_consistency_cards(conn)
    card = _card_by_name(conn, "step_two")
    assert card["pipeline_membership"], "step_two should belong to a pipeline"


def test_cards_derived_purely_from_edges(tmp_path):
    # a card has all expected keys even when sets are empty
    conn = _analyze(tmp_path, {"pkg/solo.py": "def lonely():\n    return 1\n"})
    cards.build_consistency_cards(conn)
    card = _card_by_name(conn, "lonely")
    for key in ("callers", "callees", "output_consumers", "reads", "writes", "pipeline_membership"):
        assert key in card
        assert isinstance(card[key], list)


def test_card_has_data_aware_fields(tmp_path):
    conn = _analyze(tmp_path, {"pkg/solo.py": "def lonely() -> int:\n    return 1\n"})
    cards.build_consistency_cards(conn)
    card = _card_by_name(conn, "lonely")
    for key in ("dtype_map", "columns_in", "columns_out",
                "lineage_upstream", "lineage_downstream", "profile"):
        assert key in card, f"missing {key}"
    assert isinstance(card["profile"], list)
    assert isinstance(card["dtype_map"], dict)


def test_card_profile_tag_present(tmp_path):
    src = (
        "def producer() -> list:\n    return [1]\n\n"
        "def consumer(x):\n    return x\n\n"
        "def main():\n    d = producer()\n    consumer(d)\n"
    )
    conn = _analyze(tmp_path, {"pkg/df.py": src})
    cards.build_consistency_cards(conn)
    card = _card_by_name(conn, "producer")
    assert "data-flow" in card["profile"]
