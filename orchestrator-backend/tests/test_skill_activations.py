"""Tests for SkillActivations table + db CRUD + api helpers.

Covers migration 005:
  - SkillActivations table exists with correct columns and constraints
  - source CHECK enforces enum (auto-search / explicit-mention / iron-law / deferred-load)
  - FK to Plans + Steps enforced
  - db.add_skill_activation / get_skill_activations / count_skill_uses_for_plan
  - api.record_skill_activation convenience wrapper
  - api.initialize_plan accepts skills_activated list at creation time
"""
import sqlite3

import pytest

from orchestrator import api, db


# ── Schema-level tests ────────────────────────────────────────────────────────
def test_skill_activations_table_exists(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='SkillActivations'"
    ).fetchall()
    assert len(rows) == 1


def test_skill_activations_columns_present(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(SkillActivations)").fetchall()}
    expected = {
        "activation_id", "plan_id", "step_id", "skill_name",
        "source", "reason", "activated_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_skill_activations_indexes_present(conn):
    idx_names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='SkillActivations'"
        ).fetchall()
    }
    assert "idx_skill_activations_plan" in idx_names
    assert "idx_skill_activations_skill" in idx_names


def test_source_check_constraint_rejects_invalid(conn):
    """SQL CHECK constraint should reject bad source values (defense in depth).
    Uses raw INSERT to bypass the friendlier Python-side ValueError in db.add_skill_activation."""
    db.insert_plan(conn, "p1", "g")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO SkillActivations (plan_id, skill_name, source) VALUES (?, ?, ?)",
            ("p1", "tdd", "WIBBLE"),
        )


def test_source_check_accepts_all_four_enum_values(conn):
    db.insert_plan(conn, "p1", "g")
    for src in ("auto-search", "explicit-mention", "iron-law", "deferred-load"):
        db.add_skill_activation(conn, plan_id="p1", skill_name=f"s-{src}", source=src)
    rows = db.get_skill_activations(conn, "p1")
    assert len(rows) == 4


def test_fk_to_plans_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.add_skill_activation(
            conn, plan_id="ghost-plan", skill_name="tdd", source="iron-law",
        )


def test_fk_to_steps_enforced_when_step_id_provided(conn):
    db.insert_plan(conn, "p1", "g")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_skill_activation(
            conn, plan_id="p1", skill_name="tdd", source="iron-law",
            step_id="ghost-step",
        )


# ── db.py CRUD tests ──────────────────────────────────────────────────────────
def test_add_skill_activation_minimal(conn):
    db.insert_plan(conn, "p1", "g")
    db.add_skill_activation(
        conn, plan_id="p1", skill_name="test-driven-development", source="iron-law",
    )
    rows = db.get_skill_activations(conn, "p1")
    assert len(rows) == 1
    r = rows[0]
    assert r["plan_id"] == "p1"
    assert r["skill_name"] == "test-driven-development"
    assert r["source"] == "iron-law"
    assert r["step_id"] is None
    assert r["reason"] is None
    assert r["activated_at"] is not None


def test_add_skill_activation_with_step_and_reason(conn):
    db.insert_plan(conn, "p1", "g")
    db.insert_step(conn, "p1-A", "p1", "do thing", execution_order=0)
    db.add_skill_activation(
        conn, plan_id="p1", skill_name="weekly-report", source="explicit-mention",
        step_id="p1-A", reason="user mentioned 'write my weekly report'",
    )
    rows = db.get_skill_activations(conn, "p1")
    assert len(rows) == 1
    assert rows[0]["step_id"] == "p1-A"
    assert "weekly report" in rows[0]["reason"]


def test_get_skill_activations_ordered_by_activated_at(conn):
    db.insert_plan(conn, "p1", "g")
    db.add_skill_activation(conn, plan_id="p1", skill_name="a", source="iron-law")
    db.add_skill_activation(conn, plan_id="p1", skill_name="b", source="iron-law")
    db.add_skill_activation(conn, plan_id="p1", skill_name="c", source="iron-law")
    rows = db.get_skill_activations(conn, "p1")
    assert [r["skill_name"] for r in rows] == ["a", "b", "c"]


def test_get_skill_activations_filtered_by_step(conn):
    db.insert_plan(conn, "p1", "g")
    db.insert_step(conn, "p1-A", "p1", "step a", execution_order=0)
    db.insert_step(conn, "p1-B", "p1", "step b", execution_order=1)
    db.add_skill_activation(conn, plan_id="p1", skill_name="init", source="iron-law")
    db.add_skill_activation(conn, plan_id="p1", skill_name="for-A", source="auto-search", step_id="p1-A")
    db.add_skill_activation(conn, plan_id="p1", skill_name="for-B", source="auto-search", step_id="p1-B")

    a_only = db.get_skill_activations(conn, "p1", step_id="p1-A")
    assert [r["skill_name"] for r in a_only] == ["for-A"]


def test_count_skill_uses_for_plan(conn):
    db.insert_plan(conn, "p1", "g")
    db.add_skill_activation(conn, plan_id="p1", skill_name="tdd", source="iron-law")
    db.add_skill_activation(conn, plan_id="p1", skill_name="tdd", source="iron-law")
    db.add_skill_activation(conn, plan_id="p1", skill_name="weekly-report", source="explicit-mention")
    counts = db.count_skill_uses_for_plan(conn, "p1")
    assert counts == {"tdd": 2, "weekly-report": 1}


# ── api.py wrapper tests ──────────────────────────────────────────────────────
def test_initialize_plan_records_skills_activated(conn):
    """initialize_plan with skills_activated kwarg writes them as init-time activations."""
    result = api.initialize_plan(
        conn,
        original_goal="g",
        initial_steps=["a", "b"],
        skills_activated=[
            {"skill_name": "writing-plans", "source": "iron-law"},
            {"skill_name": "test-driven-development", "source": "iron-law",
             "reason": "coding task"},
        ],
    )
    rows = db.get_skill_activations(conn, result["plan_id"])
    assert len(rows) == 2
    assert {r["skill_name"] for r in rows} == {"writing-plans", "test-driven-development"}
    # all init-time activations have step_id NULL
    assert all(r["step_id"] is None for r in rows)


def test_record_skill_activation_helper_with_step(conn):
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    api.record_skill_activation(
        conn, plan_id=r["plan_id"], skill_name="systematic-debugging",
        source="auto-search", step_id=sid, reason="hit unexpected error",
    )
    rows = db.get_skill_activations(conn, r["plan_id"])
    assert len(rows) == 1
    assert rows[0]["step_id"] == sid
    assert rows[0]["source"] == "auto-search"


def test_record_skill_activation_rejects_bad_source(conn):
    db.insert_plan(conn, "p1", "g")
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        api.record_skill_activation(
            conn, plan_id="p1", skill_name="x", source="bogus-source",
        )
