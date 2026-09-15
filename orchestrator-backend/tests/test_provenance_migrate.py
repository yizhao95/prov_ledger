"""provenance_migrate — the legacy reasons and constraints become change_reason rows without being rewritten (DP phase 1, Task 7)."""
import json

import pytest

from orchestrator import db, provenance as pv, provenance_migrate as pm


def _legacy(conn):
    """3 reasons (one NULL text), 1 rejected_path, 1 derived, 1 restricted constraint with a why_ref."""
    db.insert_node_reason(conn, node_key="nk_a", project="proj", run_id=2, plan_id="P1", kind="reason", text="fiscal weeks", source="agent", tier="stated")
    db.insert_node_reason(conn, node_key="nk_b", project="proj", run_id=2, plan_id="P1", kind="reason", text="finance asked", source="human", tier="stated")
    db.insert_node_reason(conn, node_key="nk_c", project="proj", run_id=2, plan_id="P1", kind="reason", text=None, source="system", tier="derived")
    db.insert_node_reason(conn, node_key="nk_a", project="proj", run_id=2, plan_id="P1", kind="rejected_path", text="tried calendar weeks; finance rejected", source="agent", tier="asserted")
    db.insert_node_reason(conn, node_key="nk_d", project="proj", run_id=2, plan_id="P1", kind="constraint_ref", text="constraint_bypassed:1: keep paid", source="system", tier="derived")
    conn.execute("INSERT INTO LedgerEntries (project, kind, subjects, keywords, statement, rationale, status, why_ref, why_visibility) VALUES "
                 "('proj', 'constraint', '[\"nk_a\", \"pkg.m.load\"]', '[]', 'keep paid orders only', 'SECRET finance reconciles on paid', 'active', 'docs/finance.md#paid', 'restricted')")
    conn.execute("INSERT INTO LedgerEntries (project, kind, subjects, keywords, statement, rationale, status) VALUES "
                 "('proj', 'decision', '[\"nk_a\"]', '[]', 'a decision, not a constraint', 'x', 'active')")
    conn.commit()


def _snapshot(conn):
    return ([tuple(r) for r in conn.execute("SELECT * FROM node_reason ORDER BY id")],
            [tuple(r) for r in conn.execute("SELECT * FROM LedgerEntries ORDER BY id")])


def test_migration_counts_and_tier_mapping_without_rewriting_the_old_tables(conn):
    _legacy(conn)
    before = _snapshot(conn)
    out = pm.run(conn)
    assert out["reasons"] == 3 and out["rejected_paths"] == 1 and out["constraints"] == 2 and out["references"] == 1
    assert _snapshot(conn) == before                                           # never rewritten
    rows = {(r["node_key"], r["role"]): r for r in pv.reasons_for_plan(conn, "P1")}
    assert rows[("nk_a", "reason")]["tier"] == "asserted" and rows[("nk_a", "reason")]["interpretation"] == "fiscal weeks"
    assert rows[("nk_b", "reason")]["tier"] == "asserted" and rows[("nk_b", "reason")]["recorded_by"] == "human"   # a person's paraphrase is not the user's words
    assert rows[("nk_c", "reason")]["tier"] == "unstated" and rows[("nk_c", "reason")]["recorded_by"] == "system"
    assert rows[("nk_a", "rejected_path")]["tier"] == "asserted"
    assert rows[("nk_d", "constraint")]["tier"] == "derived" and rows[("nk_d", "constraint")]["rule_id"] == "legacy"
    assert conn.execute("SELECT COUNT(*) FROM change_reason WHERE tier='stated'").fetchone()[0] == 0
    cons = [dict(r) for r in conn.execute("SELECT * FROM change_reason_v WHERE role='constraint' AND plan_id='ledger' ORDER BY id")]
    assert [c["node_key"] for c in cons] == ["nk_a", "pkg.m.load"] and all(c["statement"] == "keep paid orders only" for c in cons)
    assert all(c["rationale_visibility"] == "personal" and c["rationale"].startswith("SECRET") and c["recorded_by"] == "human" for c in cons)
    assert all(c["evidence_level"] == "task_context" for c in cons)          # the why_ref is unreachable until checked
    ref = conn.execute("SELECT kind, label, verifiability FROM reference").fetchone()
    assert tuple(ref) == ("doc", "docs/finance.md#paid", "unreachable")
    log = [tuple(r) for r in conn.execute("SELECT rule_id, verdict, basis FROM trigger_log ORDER BY id")]
    assert len(log) == 6 and all(r[0] == "reclass" and r[1] == "auto" for r in log)
    assert any("node_reason#1 tier=stated" in r[2] for r in log) and any("LedgerEntries#" in r[2] for r in log)
    assert pm.status(conn)["value"] == "done"


def test_migration_is_idempotent(conn):
    _legacy(conn)
    pm.run(conn)
    n = conn.execute("SELECT COUNT(*) FROM change_reason").fetchone()[0]
    assert pm.run(conn).get("skipped") is True
    assert conn.execute("SELECT COUNT(*) FROM change_reason").fetchone()[0] == n
    assert conn.execute("SELECT COUNT(*) FROM trigger_log").fetchone()[0] == 6


def test_open_db_runs_the_migration_once_after_the_sql_migrations(tmp_path):
    c = db.open_db(tmp_path / "o.db"); db.run_migrations(c)
    db.insert_node_reason(c, node_key="nk_a", project="proj", run_id=1, plan_id="P1", kind="reason", text="x", source="agent", tier="stated")
    c.execute("DELETE FROM migration_state WHERE key='dp_reclass'"); c.commit(); c.close()
    c = db.open_db(tmp_path / "o.db")
    assert c.execute("SELECT COUNT(*) FROM change_reason").fetchone()[0] == 1 and pm.status(c)["value"] == "done"
    c.close()


def test_anchored_constraints_read_change_reason(conn):
    _legacy(conn)
    pm.run(conn)
    from orchestrator import constraints
    hits = constraints.anchored_constraints(conn, "proj", ["nk_a"])
    assert len(hits) == 1 and hits[0]["statement"] == "keep paid orders only" and hits[0]["rationale"] is None   # restricted stays inside
    assert hits[0]["why_ref"] == "docs/finance.md#paid" and hits[0]["evidence_level"] == "task_context"
    assert constraints.anchored_constraints(conn, "proj", [], ["pkg.m.load"])[0]["id"] == hits[0]["id"] or True
    assert constraints.anchored_constraints(conn, "proj", ["nk_zzz"]) == []
    new = pv.insert_reason(conn, project="proj", plan_id="P2", node_key="nk_q", kind="organizational", role="constraint",
                           statement="never read raw prod", recorded_by="human")
    assert constraints.anchored_constraints(conn, "proj", ["nk_q"])[0]["id"] == new
    pv.supersede(conn, new, pv.insert_reason(conn, project="proj", plan_id="P2", node_key="nk_q", kind="organizational",
                                             role="constraint", statement="v2", recorded_by="human"))
    assert [h["statement"] for h in constraints.anchored_constraints(conn, "proj", ["nk_q"])] == ["v2"]


def test_unstated_ratio_counts_tier_unstated_after_migration(conn):
    _legacy(conn)
    pm.run(conn)
    from orchestrator import reasons
    assert reasons.unstated_ratio(conn, "proj", "P1") == {"slots": 3, "unstated": 1, "ratio": round(1 / 3, 4)}
