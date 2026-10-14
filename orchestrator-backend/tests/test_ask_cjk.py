"""A question in Chinese finds what was recorded, and a task reason leads to
its nodes (FL-152, FL-242).

`locate.question_tokens` only knew ASCII words, so a question asked in Chinese
gave no tokens, no candidates and an empty fact table — and the ledger skill
says an empty table is an answer. Han text has no spaces, so a word is not a
token: any two adjacent characters are. Common pairs (为什, 什么, 这个 …) carry
no subject and are dropped; a record scores once per pair it contains.

The matchers also only kept reasons with a node. Since task reasons (036) the
reason a question is most likely to match has no node of its own: it answers
for the nodes its plan changed, through reason_node or, for a plan's only
task, all of them. A matched task reason now leads to those nodes.

A Chinese question about something recorded only in English still finds
nothing by its own words; `also` carries the English words the agent adds.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import db, provenance as pv, reasons
from orchestrator.ask import locate

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

NODES = {"nk_feed": "pkg.feed.load_orders", "nk_rate": "pkg.pricing.discount_rate",
         "nk_exp": "pkg.report.export", "nk_cents": "pkg.pricing.round_cents"}
FEED = "六月起订单数据不再带折扣字段，折扣率改成按标价估算"
CENTS = "金额一律四舍五入到分"
DAILY = "keep the export daily because finance reconciles every morning"
ROUND = "round half up, as the invoices do"
LABEL = "Q2 close checklist"


@pytest.fixture
def world(conn, tmp_path):
    """P changed the feed and the rate under one task that quotes the user's
    Chinese words; Q changed the export and the cents under two English tasks;
    the cents carry an old per-node reason that quotes Chinese words."""
    graph = tmp_path / "proj-state-graph.db"
    c = ps.build(graph)
    ps.add_run(c, 1, plan_id="P0")
    for i, (k, qn) in enumerate(NODES.items(), 1):
        ps.add_snapshot(c, 1, k, qn)
        ps.add_event(c, 1, i, "node_added", k)
    for run, plan, keys in ((2, "P", ("nk_feed", "nk_rate")), (3, "Q", ("nk_exp", "nk_cents"))):
        ps.add_run(c, run, plan_id=plan, step_id=f"{plan}-REVIEW.1")
        for i, k in enumerate(keys, 1):
            ps.add_snapshot(c, run, k, NODES[k], struct_sig=f"s{run}")
            ps.add_event(c, run, i, "node_changed", k, '{"changed": ["struct_sig"]}')
    c.commit()
    c.close()
    graph = str(graph)
    for pid in ("P0", "P", "Q"):
        db.insert_plan(conn, pid, f"goal of {pid}", project="proj", project_source="declared")
    feed = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P", text=FEED,
                               occurred_at="2026-05-20 11:20:00", origin="hook")
    (p_task,) = reasons.fill_tasks(conn, project="proj", plan_id="P", run_id=2, psg_db_path=graph,
                                   tasks=[{"utterance_id": feed, "span": [0, len(FEED)]}])["reason_ids"]
    daily, side = reasons.fill_tasks(conn, project="proj", plan_id="Q", run_id=3, psg_db_path=graph,
                                     bundle="both came out of the month-end review",
                                     tasks=[{"interpretation": DAILY},
                                            {"interpretation": ROUND, "nodes": ["pkg.pricing.round_cents"]}])["reason_ids"]
    ref = pv.insert_reference(conn, project="proj", kind="doc", label=LABEL, occurred_at="2026-05-02")
    pv.link_reference(conn, daily, ref)
    cents = pv.insert_utterance(conn, session_id="s0", project="proj", plan_id="P0", text=CENTS,
                                occurred_at="2026-04-01 09:00:00", origin="hook")
    old = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_cents", kind="technical",
                           verbatim=(cents, 0, len(CENTS)), recorded_by="human")
    conn.commit()
    return {"graph": graph, "p_task": p_task, "daily": daily, "side": side, "old": old, "ref": ref}


def _qns(conn, world, question, **kw):
    return {c["qn"]: c for c in locate.candidates(conn, world["graph"], question, project="proj", **kw)}


# ── the pairs ───────────────────────────────────────────────────────────────

def test_a_chinese_question_becomes_pairs_without_the_common_ones():
    pairs = locate.question_cjk("为什么折扣率要按标价估算？")
    assert {"折扣", "扣率", "标价", "估算"} <= set(pairs)
    assert not {"为什", "什么"} & set(pairs)
    assert len(pairs) == len(set(pairs)), "each pair once"
    assert locate.question_cjk("why is the export daily?") == []


def test_mixed_text_keeps_both_kinds_of_token():
    q = "discount_rate 为什么按标价估算？"
    assert "discount_rate" in locate.question_literals(q)
    assert "标价" in locate.question_cjk(q)


# ── what a Chinese question reaches ─────────────────────────────────────────

def test_a_chinese_question_reaches_the_nodes_of_the_task_that_quotes_it(conn, world):
    by = _qns(conn, world, "订单数据为什么不再带折扣了？")
    assert {"pkg.feed.load_orders", "pkg.pricing.discount_rate"} <= set(by)
    assert f"quoted by #{world['p_task']}" in by["pkg.feed.load_orders"]["why"]
    assert "pkg.report.export" not in by


def test_a_chinese_question_reaches_a_node_through_its_own_reason(conn, world):
    by = _qns(conn, world, "金额为什么四舍五入到分？")
    assert "pkg.pricing.round_cents" in by
    assert world["old"] in by["pkg.pricing.round_cents"]["records"]


def test_common_pairs_alone_match_nothing(conn, world):
    assert _qns(conn, world, "这个为什么是这样？") == {}


def test_a_record_sharing_one_pair_of_a_longer_question_is_not_offered(conn, world):
    """One pair in common is a coincidence as often as not: a long utterance
    holds 计划 or 还能 whatever it is about."""
    by = _qns(conn, world, "折扣率为什么按标价估算？金额呢？")
    assert "pkg.pricing.discount_rate" in by
    assert "pkg.pricing.round_cents" not in by, "the cents record shares only 金额"


def test_a_record_counts_once_however_many_pairs_it_shares(conn, world):
    """As an English record counts once however many words it shares; otherwise
    a long Chinese utterance outranks every English match."""
    by = _qns(conn, world, "订单数据为什么不再带折扣了？")
    assert by["pkg.feed.load_orders"]["score"] == locate.SCORE_TEXT


# ── task reasons lead to their nodes, in any language ───────────────────────

def test_an_english_question_reaches_a_task_reasons_nodes_through_its_text(conn, world):
    by = _qns(conn, world, "why does finance reconcile every morning?")
    assert "pkg.report.export" in by
    assert f"reason #{world['daily']}" in by["pkg.report.export"]["why"]
    assert "pkg.pricing.round_cents" not in by, "the side task's node is not the daily task's"


def test_a_source_hung_on_a_task_reason_reaches_its_nodes(conn, world):
    """FL-242: a source label matched, the task reason it hangs on had no node."""
    by = _qns(conn, world, "where is the close checklist?")
    assert "pkg.report.export" in by
    assert f"source #{world['ref']} " in by["pkg.report.export"]["why"]


# ── the agent's English words ───────────────────────────────────────────────

def test_also_carries_english_words_to_an_english_only_record(conn, world):
    question = "财务为什么每天早上对账？"
    assert "pkg.report.export" not in _qns(conn, world, question)
    assert "pkg.report.export" in _qns(conn, world, question, also="finance reconciles every morning")


def _cli(conn, world, tmp_path, *argv):
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "proj", "repo": str(tmp_path), "db_path": world["graph"]}]}))
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ, ORCH_DB=conn.execute("PRAGMA database_list").fetchone()[2],
               PSG_REGISTRY_PATH=str(reg), PYTHONPATH=str(repo / "orchestrator-backend"))
    return subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv], capture_output=True, text=True,
                          env=env, cwd=str(tmp_path))


def test_ask_takes_also_on_the_command_line(conn, world, tmp_path):
    r = _cli(conn, world, tmp_path, "ask", "财务为什么每天早上对账？", "--also", "finance reconciles every morning",
             "--json", "--no-model", "--project", "proj")
    assert r.returncode == 0, r.stderr
    assert "pkg.report.export" in {c["qn"] for c in json.loads(r.stdout)["candidates"]}


def test_receipts_candidates_take_also_too(conn, world, tmp_path):
    r = _cli(conn, world, tmp_path, "receipts", "candidates", "财务为什么每天早上对账？",
             "--also", "finance reconciles every morning", "--json", "--project", "proj")
    assert r.returncode == 0, r.stderr
    assert "pkg.report.export" in {c["qn"] for c in json.loads(r.stdout)["candidates"]}
