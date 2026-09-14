"""Tests for ledger_cli.py — provLedger Phase E manual decision-memory CLI.

Mirrors test_publish_plan's subprocess+ORCH_DB pattern: each test gets an
ephemeral migrated DB and invokes the CLI as a real process.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / "scripts"
# The orchestrator import path is provided by conftest.py (it prefers the
# bundled orchestrator-backend/ so a fresh clone is self-contained).
from orchestrator import db as orch_db  # noqa: E402

# Run the CLI with the interpreter running the tests. ledger_cli.py is
# stdlib-only, so no special venv is required. (Previously hardcoded
# ~/skill-workspace/orchestrator/.venv, which broke the suite on any machine
# without that hand-made venv.)
PYBIN = Path(sys.executable)


@pytest.fixture
def orch_db_path(tmp_path):
    p = tmp_path / "orch.db"
    c = sqlite3.connect(str(p))
    orch_db.run_migrations(c)
    c.close()
    return p


def _run(args, db_path):
    env = os.environ.copy()
    env["ORCH_DB"] = str(db_path)
    return subprocess.run(
        [str(PYBIN), str(SCRIPTS / "ledger_cli.py"), *args],
        capture_output=True, text=True, env=env, timeout=20)


def test_add_decision(orch_db_path):
    res = _run([
        "add", "--project", "proj", "--kind", "decision",
        "--statement", "rolling-window split, not random",
        "--rationale", "random split leaks temporal info",
        "--subjects", "train_test_split,split",
        "--keywords", "split,rolling,temporal",
    ], orch_db_path)
    assert res.returncode == 0, res.stderr
    c = sqlite3.connect(str(orch_db_path))
    row = c.execute("SELECT project, kind, statement FROM LedgerEntries").fetchone()
    c.close()
    assert row == ("proj", "decision", "rolling-window split, not random")


def test_list_shows_active(orch_db_path):
    _run(["add", "--project", "proj", "--kind", "anti_pattern",
          "--statement", "UPC BIGINT to STRING", "--rationale", "joins broke",
          "--subjects", "upc", "--keywords", "upc,bigint,string"], orch_db_path)
    res = _run(["list", "--project", "proj"], orch_db_path)
    assert res.returncode == 0, res.stderr
    assert "UPC BIGINT to STRING" in res.stdout


def test_invalid_kind_nonzero(orch_db_path):
    res = _run(["add", "--project", "proj", "--kind", "bogus",
                "--statement", "s", "--rationale", "r"], orch_db_path)
    assert res.returncode != 0


def test_add_constraint_with_why_ref_and_visibility(orch_db_path):
    r = _run(["add", "--project", "proj", "--kind", "constraint",
              "--statement", "exclude region X", "--rationale", "legal hold",
              "--subjects", "nk_abc,orders.region", "--why-ref", "https://wiki/decisions/42",
              "--why-visibility", "restricted", "--plan-id", "plan-9"], orch_db_path)
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(str(orch_db_path))
    row = c.execute("SELECT kind, why_ref, why_visibility, plan_id, subjects FROM LedgerEntries").fetchone()
    assert row == ("constraint", "https://wiki/decisions/42", "restricted", "plan-9", '["nk_abc", "orders.region"]')
    r2 = _run(["add", "--project", "proj", "--kind", "constraint", "--statement", "s",
               "--why-visibility", "secret"], orch_db_path)
    assert r2.returncode != 0


def test_add_on_a_fresh_db_runs_the_migrations_first(tmp_path):
    """FL-025: the ledger CLI opened the orchestrator DB raw, so on a DB that
    no other script had touched yet 'add' died with 'no such table:
    LedgerEntries'. Every write path migrates on open (FL-021) — this one too."""
    fresh = tmp_path / "never-opened.db"
    assert not fresh.exists()
    r = _run(["add", "--project", "p", "--kind", "constraint", "--statement", "keep it",
              "--subjects", "nk_1", "--why-visibility", "restricted"], fresh)
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(str(fresh))
    assert c.execute("SELECT COUNT(*) FROM LedgerEntries WHERE project='p'").fetchone()[0] == 1
    assert "project" in {row[1] for row in c.execute("PRAGMA table_info(Plans)")}   # fully migrated, not just one table
    c.close()


# ── phase 5 Task 3: declarative constraint import ─────────────────────────────

def _run_env(args, db_path, extra=None):
    env = os.environ.copy()
    env["ORCH_DB"] = str(db_path)
    env.update(extra or {})
    return subprocess.run([str(PYBIN), str(SCRIPTS / "ledger_cli.py"), *args],
                          capture_output=True, text=True, env=env, timeout=20)


def _ext_file(tmp_path, constraints):
    p = tmp_path / "provledger-extensions.json"
    p.write_text(json.dumps({"version": 1, "constraints": constraints}))
    return str(p)


def _rows(db_path):
    c = sqlite3.connect(str(db_path))
    rows = [(r[0], json.loads(r[1]), r[2], r[3]) for r in
            c.execute("SELECT statement, subjects, why_visibility, status FROM LedgerEntries ORDER BY id")]
    c.close()
    return rows


def test_import_constraints_twice_second_all_skipped(orch_db_path, tmp_path):
    f = _ext_file(tmp_path, [
        {"statement": "clean() must keep dropping zero-quantity rows", "subjects": ["pkg.pipeline.clean"],
         "rationale": "finance reconciles returns separately", "why_visibility": "restricted"},
        {"project": "proj", "statement": "orders.region != 'X' must stay excluded", "subjects": ["orders.region"],
         "keywords": ["scope-change"]},
    ])
    r = _run_env(["import", f, "--project", "proj"], orch_db_path)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["imported"] == 2 and out["skipped"] == 0
    assert sorted(out["unresolved_subjects"]) == ["orders.region", "pkg.pipeline.clean"]   # no graph registered
    rows = _rows(orch_db_path)
    assert [s for s, *_ in rows] == ["clean() must keep dropping zero-quantity rows", "orders.region != 'X' must stay excluded"]
    assert rows[0][1] == ["pkg.pipeline.clean"] and rows[0][2] == "restricted"
    r2 = _run_env(["import", f, "--project", "proj"], orch_db_path)
    assert r2.returncode == 0, r2.stderr
    out2 = json.loads(r2.stdout.strip().splitlines()[-1])
    assert out2["imported"] == 0 and out2["skipped"] == 2
    assert len(_rows(orch_db_path)) == 2


def test_import_declared_project_must_match(orch_db_path, tmp_path):
    f = _ext_file(tmp_path, [{"project": "other", "statement": "s", "subjects": ["a"]}])
    r = _run_env(["import", f, "--project", "proj"], orch_db_path)
    assert r.returncode == 1 and "project" in r.stderr and "other" in r.stderr
    assert _rows(orch_db_path) == []


def test_import_resolves_subjects_through_the_registered_graph(orch_db_path, tmp_path):
    g = tmp_path / "g.db"
    c = sqlite3.connect(str(g))
    c.executescript("CREATE TABLE node_snapshot (id INTEGER PRIMARY KEY, run_id INTEGER, node_key TEXT, qualified_name TEXT);"
                    "INSERT INTO node_snapshot (run_id, node_key, qualified_name) VALUES (1, 'nk_clean', 'pkg.pipeline.clean');")
    c.commit(); c.close()
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "proj", "repo": str(tmp_path), "db_path": str(g), "commit_sha": "c"}]}))
    f = _ext_file(tmp_path, [{"statement": "keep the filter", "subjects": ["pkg.pipeline.clean", "orders.region"]}])
    r = _run_env(["import", f, "--project", "proj"], orch_db_path, {"PSG_REGISTRY_PATH": str(reg)})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["unresolved_subjects"] == ["orders.region"]
    (_, subjects, _, _), = _rows(orch_db_path)
    assert subjects == ["pkg.pipeline.clean", "orders.region", "nk_clean"]       # original text kept, node_key added
    # idempotence keys on the ORIGINAL subjects, so a re-import after the graph changed still skips
    r2 = _run_env(["import", f, "--project", "proj"], orch_db_path, {"PSG_REGISTRY_PATH": str(reg)})
    assert json.loads(r2.stdout.strip().splitlines()[-1])["skipped"] == 1


def test_imported_restricted_rationale_never_leaves_the_ledger(orch_db_path, tmp_path):
    import ledger_store
    f = _ext_file(tmp_path, [{"statement": "s", "subjects": ["pkg.pipeline.clean"], "rationale": "SECRET",
                              "why_ref": "https://wiki/1", "why_visibility": "restricted"}])
    assert _run_env(["import", f, "--project", "proj"], orch_db_path).returncode == 0
    c = sqlite3.connect(str(orch_db_path)); c.row_factory = sqlite3.Row
    got = ledger_store.constraints_for(c, "proj", [], qualified_names=["pkg.pipeline.clean"])
    c.close()
    assert len(got) == 1 and got[0]["rationale"] is None and got[0]["why_ref"] == "https://wiki/1"
