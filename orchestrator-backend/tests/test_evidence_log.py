"""A6: `evidence_log` — so a blank can explain itself.

A reason with no source can be blank for three different reasons, and the
difference is the whole point: nobody went looking, somebody looked and the
window held nothing, or the look started and ran out of time. Collapsed into one
"no evidence" they are indistinguishable, and a system that degrades silently is
the thing this product exists to prevent — so one row per slot, append-only, with
the tier the slot landed at beside it.
"""
import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import db, evidence, provenance as pv

REPO = Path(__file__).resolve().parents[2]
COLUMNS = ("plan_id", "node_key", "reason_tier", "evidence_level", "searched", "tool_hint",
           "outcome", "elapsed_ms", "at")


@pytest.fixture
def plan(conn):
    db.insert_plan(conn, "P1", "drop EMEA from the Q3 rollup", project="proj", project_source="declared")
    return "P1"


def _cli(conn, *argv):
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(REPO / "orchestrator-backend"))
    return subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv],
                          capture_output=True, text=True, env=env, cwd=str(REPO))


def _rows(conn, plan_id="P1"):
    return [dict(r) for r in conn.execute("SELECT * FROM evidence_log WHERE plan_id = ? ORDER BY id", (plan_id,))]


# ── (a) the columns the audit needs ─────────────────────────────────────────

def test_the_table_carries_every_column_the_audit_asks_for(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(evidence_log)")}
    assert set(COLUMNS) <= cols, sorted(set(COLUMNS) - cols)


def test_one_row_per_slot(conn, plan):
    for key, outcome in (("nk_a", "attached"), ("nk_b", "found_nothing"), ("nk_c", "not_searched")):
        evidence.record(conn, plan_id="P1", node_key=key, outcome=outcome)
    rows = _rows(conn)
    assert [r["node_key"] for r in rows] == ["nk_a", "nk_b", "nk_c"]
    assert len(rows) == 3


def test_log_for_plan_reads_back_only_that_plans_rows(conn, plan):
    db.insert_plan(conn, "P2", "another plan", project="proj", project_source="declared")
    evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="found_nothing")
    evidence.record(conn, plan_id="P2", node_key="nk_z", outcome="attached")
    assert [r["node_key"] for r in evidence.log_for_plan(conn, "P1")] == ["nk_a"]
    assert [r["node_key"] for r in evidence.log_for_plan(conn, "P2")] == ["nk_z"]


# ── (b) three blanks, three different rows ──────────────────────────────────

def test_searched_and_found_nothing_timed_out_and_never_searched_are_distinguishable(conn, plan):
    evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="found_nothing",
                    tool_hint="outlook:search", elapsed_ms=820)
    evidence.record(conn, plan_id="P1", node_key="nk_b", outcome="timed_out",
                    tool_hint="outlook:search", elapsed_ms=300000)
    evidence.record(conn, plan_id="P1", node_key="nk_c", outcome="not_searched")
    by_node = {r["node_key"]: r for r in _rows(conn)}
    assert by_node["nk_a"]["outcome"] == "found_nothing" and by_node["nk_a"]["searched"] == 1
    assert by_node["nk_b"]["outcome"] == "timed_out" and by_node["nk_b"]["searched"] == 1
    assert by_node["nk_c"]["outcome"] == "not_searched" and by_node["nk_c"]["searched"] == 0
    assert len({r["outcome"] for r in _rows(conn)}) == 3
    assert {"found_nothing", "timed_out", "not_searched"} <= set(evidence.OUTCOMES)


def test_a_blank_reason_can_be_asked_which_kind_of_blank_it_is(conn, plan):
    """The question this table exists for. `unstated` with no pointer is the same
    row whether the mailbox was empty or never opened; only the log separates them."""
    evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="not_searched",
                    reason_tier="unstated", evidence_level="unstated")
    evidence.record(conn, plan_id="P1", node_key="nk_b", outcome="found_nothing",
                    reason_tier="unstated", evidence_level="unstated", tool_hint="outlook:search")
    same = {(r["reason_tier"], r["evidence_level"]) for r in _rows(conn)}
    assert same == {("unstated", "unstated")}, "both rows are blank in the ledger"
    assert {r["node_key"]: r["outcome"] for r in _rows(conn)} == {"nk_a": "not_searched", "nk_b": "found_nothing"}


def test_searched_is_derived_from_the_outcome_when_it_is_not_given(conn, plan):
    for key, outcome, expected in (("nk_a", "attached", 1), ("nk_b", "found_nothing", 1),
                                   ("nk_c", "timed_out", 1), ("nk_d", "not_searched", 0)):
        evidence.record(conn, plan_id="P1", node_key=key, outcome=outcome)
    assert [r["searched"] for r in _rows(conn)] == [1, 1, 1, 0]


def test_a_searched_flag_that_contradicts_the_outcome_is_refused(conn, plan):
    with pytest.raises(ValueError, match="searched"):
        evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="found_nothing", searched=False)
    with pytest.raises(ValueError, match="searched"):
        evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="not_searched", searched=True)
    assert _rows(conn) == []


def test_the_database_itself_refuses_the_contradiction(conn, plan):
    """Not only the writer: a row inserted around `record` cannot claim it never
    searched and found nothing at the same time."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO evidence_log (plan_id, node_key, searched, outcome) "
                     "VALUES ('P1', 'nk_a', 0, 'found_nothing')")


def test_an_outcome_outside_the_four_is_refused_and_writes_nothing(conn, plan):
    with pytest.raises(ValueError, match="outcome"):
        evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="probably_fine")
    assert _rows(conn) == []
    assert len(evidence.OUTCOMES) == 4


# ── (c) the tier the slot landed at ─────────────────────────────────────────

@pytest.mark.parametrize("tier", list(pv.TIERS))
def test_a_slot_that_produced_a_reason_records_which_tier_it_landed_at(conn, plan, tier):
    evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="found_nothing", reason_tier=tier)
    assert _rows(conn)[0]["reason_tier"] == tier


def test_the_tier_and_the_level_come_from_the_reason_row_itself(conn, plan):
    uid = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P1",
                              text="drop EMEA from the Q3 rollup", occurred_at="2026-09-26 09:00:00",
                              origin="hook")
    rid = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical",
                           verbatim=(uid, 0, 14))
    ref = pv.insert_reference(conn, project="proj", kind="email", label="re: Q3 · sarah",
                              occurred_at="2026-09-26 09:00:00", uri="https://outlook/items/AAQk")
    pv.link_reference(conn, rid, ref)
    row = pv.get_reason(conn, rid)
    evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="attached",
                    reason_tier=row["tier"], evidence_level=row["evidence_level"],
                    tool_hint="outlook:search", elapsed_ms=1200)
    got = _rows(conn)[0]
    assert (got["reason_tier"], got["evidence_level"]) == ("stated", "linked")
    assert (got["tool_hint"], got["elapsed_ms"]) == ("outlook:search", 1200)


def test_a_tier_outside_the_four_is_refused(conn, plan):
    with pytest.raises(ValueError, match="tier"):
        evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="attached", reason_tier="probable")
    assert _rows(conn) == []


def test_an_evidence_level_outside_the_four_is_refused(conn, plan):
    with pytest.raises(ValueError, match="evidence_level"):
        evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="attached", evidence_level="pretty_good")
    assert _rows(conn) == []


def test_a_negative_elapsed_time_is_refused(conn, plan):
    with pytest.raises(ValueError, match="elapsed_ms"):
        evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="timed_out", elapsed_ms=-1)
    assert _rows(conn) == []


# ── (d) append-only, the same way its neighbours are ────────────────────────

def test_the_table_is_append_only(conn, plan):
    evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="found_nothing")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE evidence_log SET outcome = 'attached' WHERE plan_id = 'P1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM evidence_log WHERE plan_id = 'P1'")
    assert _rows(conn)[0]["outcome"] == "found_nothing"


def test_the_triggers_have_the_same_shape_as_its_neighbours(conn):
    def shape(table):
        return [re.sub(r"\s+", " ", sql).strip() for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ? ORDER BY name", (table,))]
    mine, neighbour = shape("evidence_log"), shape("significance_log")
    assert len(mine) == 2, mine
    assert [s.replace("evidence_log", "TBL") for s in mine] == \
           [s.replace("significance_log", "TBL") for s in neighbour]


def test_a_second_look_at_the_same_slot_is_another_row_not_a_correction(conn, plan):
    evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="found_nothing", tool_hint="outlook:search")
    evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="attached", tool_hint="teams:search")
    assert [(r["outcome"], r["tool_hint"]) for r in _rows(conn)] == \
        [("found_nothing", "outlook:search"), ("attached", "teams:search")]


def test_the_clock_is_the_databases_own(conn, plan):
    assert "at" not in inspect.signature(evidence.record).parameters, \
        "`at` is the database's reading, never a caller's claim"
    now = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%S', 'now')").fetchone()[0]
    evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="found_nothing")
    assert _rows(conn)[0]["at"] >= now


# ── (e) the command the host writes through ─────────────────────────────────

def test_the_command_records_one_outcome(conn, plan):
    r = _cli(conn, "review", "evidence-log", "--plan", "P1", "--node", "nk_a", "--outcome", "found_nothing",
             "--tool-hint", "outlook:search", "--elapsed-ms", "820", "--reason-tier", "unstated", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["outcome"] == "found_nothing" and out["searched"] is True
    got = _rows(conn)[0]
    assert (got["outcome"], got["tool_hint"], got["elapsed_ms"], got["reason_tier"]) == \
        ("found_nothing", "outlook:search", 820, "unstated")


def test_the_command_lists_the_log_of_a_plan(conn, plan):
    evidence.record(conn, plan_id="P1", node_key="nk_a", outcome="not_searched")
    evidence.record(conn, plan_id="P1", node_key="nk_b", outcome="timed_out", elapsed_ms=300000)
    r = _cli(conn, "review", "evidence-log", "--plan", "P1", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert [e["outcome"] for e in out["entries"]] == ["not_searched", "timed_out"]


def test_a_bad_outcome_is_refused_with_exit_2_and_writes_nothing(conn, plan):
    r = _cli(conn, "review", "evidence-log", "--plan", "P1", "--node", "nk_a", "--outcome", "probably_fine")
    assert r.returncode == 2, r.stdout
    assert _rows(conn) == []


def test_recording_needs_both_the_node_and_the_outcome(conn, plan):
    r = _cli(conn, "review", "evidence-log", "--plan", "P1", "--node", "nk_a")
    assert r.returncode == 2, r.stdout
    assert "--outcome" in r.stderr, r.stderr
    assert _rows(conn) == []
