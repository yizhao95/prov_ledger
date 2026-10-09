"""A supersede chain reads as its newest row (FL-238, FL-239).

`provenance.supersede(old, new)` is the one UPDATE the ledger allows: the old
row keeps its own words and points at its successor. Constraint readers have
always skipped a superseded row; reason and rejected-path readers printed both,
so a correction appended to fix a false `stated` row left the false row on
screen. Every reader that shows these rows now shows the newest one and says
which row it corrects; `provledger record <old>` still prints the old row."""
import sys
from pathlib import Path

import pytest

from orchestrator import context_pack as cp, graph_view, provenance as pv, triggers, why
from orchestrator.ask import facts as fx

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

QUOTE = "keep load_orders weekly please"
FAILED = "COMMAND: bash scripts/run_tests.sh backend — exit_code=1"


def _snap(c, run, key, qn, lo, hi):
    c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, line_start, line_end, "
              "struct_sig, dataflow_trivial, attrs_json) VALUES (?, ?, 'function', ?, 'pkg/m.py', ?, ?, 's', 1, '{}')",
              (run, key, qn, lo, hi))


@pytest.fixture
def graph(tmp_path):
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P1")
    _snap(c, 1, "nk_a", "pkg.m.load_orders", 10, 40)
    ps.add_event(c, 1, 1, "node_added", "nk_a")
    _snap(c, 1, "nk_x", "pkg.m.clean", 50, 60)
    ps.add_event(c, 1, 2, "node_added", "nk_x")
    c.commit()
    c.close()
    return str(path)


@pytest.fixture
def chain(conn):
    """A stated R0 row on nk_a corrected by an unstated one, and an R6 row
    re-anchored from nk_a to nk_x."""
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P1", text=QUOTE,
                            occurred_at="2026-09-15 09:00:00")
    old = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical",
                           verbatim=(u, 0, len(QUOTE)), rule_id="R0", recorded_by="system")
    new = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical", recorded_by="system")
    pv.supersede(conn, old, new)
    old2 = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical", role="rejected_path",
                            interpretation=FAILED, rule_id="R6", recorded_by="system")
    new2 = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_x", kind="technical", role="rejected_path",
                            interpretation=FAILED, rule_id="R6", recorded_by="system")
    pv.supersede(conn, old2, new2)
    return {"old": old, "new": new, "old2": old2, "new2": new2}


def test_the_shared_record_read_returns_the_newest_row_and_what_it_corrects(conn, chain):
    recs = {r["id"]: r for r in cp._records(conn, "proj", ["nk_a", "nk_x"])}
    assert set(recs) == {chain["new"], chain["new2"]}
    assert cp._slim(recs[chain["new2"]])["corrects"] == [chain["old2"]]
    assert cp._slim(recs[chain["new"]])["corrects"] == [chain["old"]]


def test_why_drops_the_corrected_quote_and_says_what_the_new_row_corrects(conn, graph, chain):
    a = why.why(conn, project="proj", target="pkg.m.load_orders", psg_db_path=graph, record=False, all_records=True)
    assert QUOTE not in a["text"] and FAILED not in a["text"]
    assert "rejected 0 · pending 1" in a["text"]
    p = why.why(conn, project="proj", target="pkg.m.load_orders", psg_db_path=graph, record=False, pending_only=True)
    assert f"#{chain['new']} · unstated" in p["text"] and f"corrects #{chain['old']}" in p["text"]
    x = why.why(conn, project="proj", target="pkg.m.clean", psg_db_path=graph, record=False)
    assert FAILED in x["text"] and f"corrects #{chain['old2']}" in x["text"]


def test_the_project_wide_reads_skip_superseded_rows(conn, graph, chain, tmp_path):
    slot = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_x", kind="technical", recorded_by="system")
    answer = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_x", kind="technical",
                              interpretation="clean drops test rows", recorded_by="agent")
    pv.supersede(conn, slot, answer)
    assert slot not in {r["id"] for r in why.pending(conn, project="proj")}
    hits, _ = why.search(conn, project="proj", query="load_orders weekly")
    assert chain["old"] not in {h["id"] for h in hits}
    why.export_md(conn, project="proj", out_dir=str(tmp_path / "md"), psg_db_path=graph)
    written = "".join(p.read_text() for p in (tmp_path / "md").glob("*.md"))
    assert QUOTE not in written and written.count(FAILED) == 1


def test_the_fact_table_and_receipts_show_the_newest_row(conn, graph, chain):
    ft = fx.facts(conn, graph, ["pkg.m.load_orders", "pkg.m.clean"], project="proj")
    text = fx.render(ft)
    assert QUOTE not in text and text.count(FAILED) == 1
    assert f"corrects #{chain['old']}" in text and f"corrects #{chain['old2']}" in text


def test_the_badge_and_the_graph_counts_count_a_chain_once(conn, chain):
    badge = {r["node_key"]: r for r in conn.execute("SELECT node_key, reasons, rejected_paths FROM node_badge_v")}
    assert (badge["nk_a"]["reasons"], badge["nk_a"]["rejected_paths"]) == (0, 0)
    assert badge["nk_x"]["rejected_paths"] == 1
    assert graph_view._record_counts(conn, "proj") == {"nk_a": 1, "nk_x": 1}


def test_the_checklist_of_rule_answers_leaves_out_a_corrected_one(conn, chain):
    assert all(r["basis"] != QUOTE for r in triggers.auto_filled(conn, "P1"))


def test_the_record_read_still_prints_the_old_row(conn, chain):
    from orchestrator import record_read
    doc = record_read.record(conn, f"#{chain['old']}")
    assert doc["superseded_by"] == chain["new"]


def test_the_export_bundle_writes_one_node_file_line_per_chain(conn, graph, chain, tmp_path):
    from orchestrator import export
    export.bundle(conn, "proj", str(tmp_path / "out"), psg_db_path=graph, log=False)
    nodes = "".join(p.read_text() for p in (tmp_path / "out" / "proj" / "nodes").glob("*.md"))
    assert nodes.count(FAILED) == 1


def test_the_receipts_timeline_says_what_a_row_corrects(conn, graph, chain):
    from orchestrator.ask import receipts
    text = receipts.render_text(receipts.facts(conn, project="proj", chosen=["pkg.m.load_orders", "pkg.m.clean"],
                                               psg_db_path=graph))
    assert QUOTE not in text and text.count(FAILED) == 1
    assert f"corrects #{chain['old']}" in text and f"corrects #{chain['old2']}" in text
