"""Tests for ledger_store — provLedger Phase E manual decision-memory store.

ledger_store is stdlib-only (sqlite3+json) and imports nothing from the
orchestrator package. Tests build a migrated DB via the orchestrator's
run_migrations (so the real LedgerEntries schema is exercised).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
ORCH_ROOT = Path.home() / "skill-workspace" / "orchestrator"
sys.path.insert(0, str(ORCH_ROOT))

import ledger_store  # noqa: E402
from orchestrator import db as orch_db  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "led.db"))
    c.row_factory = sqlite3.Row
    orch_db.run_migrations(c)
    return c


def test_add_entry_records_plan_id(conn):
    # SK-D1: provenance plan recorded on the entry.
    eid = ledger_store.add_entry(
        conn, project="proj", kind="decision", statement="s", rationale="r",
        plan_id="plan-123")
    row = conn.execute("SELECT plan_id FROM LedgerEntries WHERE id=?", (eid,)).fetchone()
    assert row["plan_id"] == "plan-123"


def test_supersede_records_lineage(conn):
    # SK-D1: superseded_by + updated_at form an auditable lineage.
    old = ledger_store.add_entry(conn, project="p", kind="decision", statement="old")
    new = ledger_store.add_entry(conn, project="p", kind="decision", statement="new")
    ledger_store.supersede_entry(conn, old, superseded_by=new)
    row = conn.execute("SELECT status, superseded_by, updated_at FROM LedgerEntries WHERE id=?",
                       (old,)).fetchone()
    assert row["status"] == "superseded"
    assert row["superseded_by"] == new
    assert row["updated_at"] is not None


def test_record_hit_writes_read_hit_not_hit_count(conn):
    """DP phase 2: surfacing a constraint is a read_hit (moment plan) on its
    change_reason twin; LedgerEntries.hit_count stops moving. A decision has
    no twin and records nothing."""
    cid = ledger_store.add_entry(conn, project="p", kind="constraint", statement="keep paid", subjects=["nk_a"])
    did = ledger_store.add_entry(conn, project="p", kind="decision", statement="s")
    ledger_store.record_hit(conn, cid)
    ledger_store.record_hit(conn, cid)
    ledger_store.record_hit(conn, did)
    assert [r[0] for r in conn.execute("SELECT hit_count FROM LedgerEntries ORDER BY id")] == [0, 0]
    rows = conn.execute("SELECT reason_id, moment FROM read_hit ORDER BY id").fetchall()
    assert len(rows) == 2 and {r[1] for r in rows} == {"plan"}
    assert conn.execute("SELECT statement FROM change_reason WHERE id=?", (rows[0][0],)).fetchone()[0] == "keep paid"


def test_add_entry_round_trip(conn):
    eid = ledger_store.add_entry(
        conn, project="proj", kind="decision",
        statement="rolling-window split, not random",
        rationale="random split leaks temporal info",
        subjects=["train_test_split", "split"],
        keywords=["split", "rolling", "temporal"],
        source="manual")
    assert isinstance(eid, int) and eid > 0
    rows = ledger_store.get_entries(conn, "proj")
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "decision"
    assert r["statement"].startswith("rolling-window")
    assert "split" in r["subjects"]
    assert "temporal" in r["keywords"]


def test_invalid_kind_rejected(conn):
    with pytest.raises(ValueError):
        ledger_store.add_entry(conn, project="p", kind="bogus",
                               statement="s", rationale="r")


def test_active_only_by_default(conn):
    a = ledger_store.add_entry(conn, project="p", kind="decision",
                               statement="keep", rationale="r")
    b = ledger_store.add_entry(conn, project="p", kind="anti_pattern",
                               statement="drop", rationale="r")
    ledger_store.supersede_entry(conn, b)
    active = ledger_store.get_entries(conn, "p")
    assert {e["id"] for e in active} == {a}
    allrows = ledger_store.get_entries(conn, "p", include_superseded=True)
    assert {e["id"] for e in allrows} == {a, b}


def test_query_entries_deterministic_order(conn):
    ids = [ledger_store.add_entry(conn, project="p", kind="decision",
                                  statement=f"s{i}", rationale="r") for i in range(3)]
    rows = ledger_store.query_entries(conn, "p")
    # deterministic (by created_at then id); all present
    assert {r["id"] for r in rows} == set(ids)
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)


def test_other_project_isolated(conn):
    ledger_store.add_entry(conn, project="p1", kind="decision",
                           statement="s", rationale="r")
    assert ledger_store.get_entries(conn, "p2") == []


# ── 3.1-B: constraints anchored to node keys (E4-1 … E4-4) ───────────────────

def _constraint(conn, subjects, **kw):
    base = dict(project="proj", kind="constraint", statement="exclude region X from the rollup",
                rationale="legal hold on region X since 2024-Q3", subjects=subjects,
                why_ref="https://wiki/decisions/42")
    base.update(kw)
    return ledger_store.add_entry(conn, **base)


def test_add_constraint_and_lookup_by_node_key(conn):
    cid = _constraint(conn, ["nk_abc", "orders.region"])
    got = ledger_store.constraints_for(conn, "proj", ["nk_abc"])
    assert [c["id"] for c in got] == [cid]
    c = got[0]
    assert c["statement"].startswith("exclude region") and c["rationale"].startswith("legal hold")
    assert c["why_ref"] == "https://wiki/decisions/42" and c["why_visibility"] == "shared"
    assert c["subjects"] == ["nk_abc", "orders.region"] and c["hit_count"] == 0
    assert ledger_store.constraints_for(conn, "proj", ["nk_zzz"]) == []
    assert ledger_store.constraints_for(conn, "proj", []) == []
    assert ledger_store.constraints_for(conn, "other", ["nk_abc"]) == []


def test_restricted_hides_rationale_keeps_why_ref(conn):
    _constraint(conn, ["nk_abc"], why_visibility="restricted")
    c = ledger_store.constraints_for(conn, "proj", ["nk_abc"])[0]
    assert c["rationale"] is None and c["why_ref"] == "https://wiki/decisions/42"
    assert c["why_visibility"] == "restricted"
    # the row itself still holds the rationale — only the lookup hides it
    assert conn.execute("SELECT rationale FROM LedgerEntries").fetchone()[0].startswith("legal hold")


def test_constraints_for_ignores_lexical_overlap(conn):
    """statement/keywords mention the target; subjects do not carry its key -> no hit."""
    _constraint(conn, ["nk_other"], statement="nk_abc must keep the region filter", keywords=["nk_abc", "region"])
    assert ledger_store.constraints_for(conn, "proj", ["nk_abc"]) == []
    # decisions / superseded constraints are not constraints_for hits either
    ledger_store.add_entry(conn, project="proj", kind="decision", statement="s", subjects=["nk_abc"])
    sid = _constraint(conn, ["nk_abc"])
    ledger_store.supersede_entry(conn, sid)
    assert ledger_store.constraints_for(conn, "proj", ["nk_abc"]) == []


def test_add_entry_rejects_bad_kind_or_visibility(conn):
    with pytest.raises(ValueError):
        ledger_store.add_entry(conn, project="proj", kind="rule", statement="s")
    with pytest.raises(ValueError):
        ledger_store.add_entry(conn, project="proj", kind="constraint", statement="s", why_visibility="secret")


def test_e4_4_no_hardcoded_taxonomy():
    """No category taxonomy ships in the package: besides VALID_KINDS and
    VALID_VISIBILITY there is no other upper-case tuple/list constant, and no
    CATEGOR*/TAXONOMY identifier, in ledger_store or ledger_cli."""
    import inspect, re
    import ledger_cli
    for mod in (ledger_store, ledger_cli):
        src = inspect.getsource(mod)
        assert not re.search(r"CATEGOR|TAXONOM", src), mod.__name__
        consts = re.findall(r"^([A-Z][A-Z_]+)\s*=\s*[\(\[]", src, re.M)
        assert set(consts) <= {"VALID_KINDS", "VALID_VISIBILITY"}, (mod.__name__, consts)


def test_constraints_for_matches_qualified_names_too(conn):
    cid = _constraint(conn, ["nk_abc", "orders.region"])
    assert [c["id"] for c in ledger_store.constraints_for(conn, "proj", [], qualified_names=["orders.region"])] == [cid]
    assert [c["id"] for c in ledger_store.constraints_for(conn, "proj", ["nk_zzz"], qualified_names=["orders.region"])] == [cid]
    assert ledger_store.constraints_for(conn, "proj", [], qualified_names=["nope"]) == []
    assert ledger_store.constraints_for(conn, "proj", [], qualified_names=[]) == []
