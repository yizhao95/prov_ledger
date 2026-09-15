"""`provledger note` — after-the-fact verbal records (DP phase 1, Task 6; B2, B5)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import cli, provenance as pv

REPO = Path(__file__).resolve().parents[2]
BANNED = ("追责", "甩锅", "防老板", "呈堂")


def _run(conn, *argv, env_extra=None):
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(REPO / "orchestrator-backend"))
    env.update(env_extra or {})
    r = subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv], capture_output=True, text=True, env=env, cwd=str(REPO))
    return r


@pytest.fixture
def registry(tmp_path, monkeypatch):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": "proj", "repo": str(REPO), "db_path": str(tmp_path / "g.db"), "commit_sha": "c"}]}))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(p))
    return {"PSG_REGISTRY_PATH": str(p)}


def test_b5_note_keeps_occurred_at_apart_from_recorded_at(conn, registry):
    r = _run(conn, "note", "finance asked for fiscal weeks", "--at", "2026-09-10 14:30", "--project", "proj", env_extra=registry)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    row = pv.get_utterance(conn, out["utterance_id"])
    assert row["text"] == "finance asked for fiscal weeks" and row["occurred_at"] == "2026-09-10 14:30:00"
    assert row["recorded_at"] != row["occurred_at"] and row["recorded_at"] > row["occurred_at"]
    assert row["project"] == "proj" and row["plan_id"] is None and row["session_id"] == "note"
    r = _run(conn, "note", "x", "--at", "yesterday", "--project", "proj", env_extra=registry)
    assert r.returncode != 0 and "--at" in r.stderr


def test_b2_verbal_reference_without_uri_and_unreachable_email(conn, registry):
    r = _run(conn, "note", "we agreed in the hallway", "--at", "2026-09-10", "--project", "proj",
             "--ref", "kind=verbal,label=hallway with the CFO", "--ref", "kind=email,label=re: weeks",
             "--ref", "kind=ticket,label=DATA-42,uri=https://tracker/DATA-42", env_extra=registry)
    assert r.returncode == 0, r.stderr
    ids = json.loads(r.stdout)["reference_ids"]
    rows = [tuple(x) for x in conn.execute("SELECT kind, uri, verifiability FROM reference WHERE id IN (?,?,?) ORDER BY id", ids)]
    assert rows == [("verbal", None, "verbal"), ("email", None, "unreachable"), ("ticket", "https://tracker/DATA-42", "linked")]


def test_node_makes_a_stated_reason_with_evidence_level_verbal(conn, registry):
    r = _run(conn, "note", "keep load_orders on paid orders only, finance reconciles on paid", "--at", "2026-09-10 09:00",
             "--project", "proj", "--plan", "P9", "--node", "nk_a", env_extra=registry)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    reason = pv.get_reason(conn, out["reason_id"])
    assert reason["tier"] == "stated" and reason["evidence_level"] == "verbal" == out["evidence_level"]
    assert reason["recorded_by"] == "human" and reason["node_key"] == "nk_a" and reason["plan_id"] == "P9"
    assert (reason["verbatim_start"], reason["verbatim_end"]) == (0, len("keep load_orders on paid orders only, finance reconciles on paid"))
    assert reason["kind"] == "organizational" and reason["occurred_at"] == "2026-09-10 09:00:00"
    r = _run(conn, "note", "with a linked source", "--at", "2026-09-10", "--project", "proj", "--node", "nk_b",
             "--ref", "kind=doc,label=decision doc,uri=https://docs/1", env_extra=registry)
    assert pv.get_reason(conn, json.loads(r.stdout)["reason_id"])["evidence_level"] == "linked"


def test_help_exits_zero_and_uses_neutral_wording():
    for argv in (["--help"], ["note", "--help"], ["metrics", "--help"], ["reasons", "--help"]):
        r = subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv], capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=str(REPO / "orchestrator-backend")))
        assert r.returncode == 0, (argv, r.stderr)
        assert not any(w in r.stdout for w in BANNED), argv
    assert "note" in cli.build_parser().format_help()


def test_reasons_reclass_status_prints_counts(conn):
    r = _run(conn, "reasons", "reclass-status")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["dp_reclass"] == "done" and out["change_reason_by_tier"] == {} and out["node_reason_rows"] == 0   # reclass ran on open, nothing to move
