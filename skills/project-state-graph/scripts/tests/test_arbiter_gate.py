"""Phase 7 Task 2 (PSG side): the calibration export, and the gate that decides
whether cli.run hands an arbiter to history.resolve. Refusal is the default."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from analyzer import calibration_export, cli, history, py_ast, store, walker
from analyzer._host import testing as _testing

import importlib
cal = importlib.import_module(_testing.__name__ + ".calibration")
HEUR = "provledger.testing.heuristic_arbiter:HeuristicArbiter" if _testing.__name__.startswith("provledger") \
    else "orchestrator.testing.heuristic_arbiter:HeuristicArbiter"

TWINS = {"pkg/__init__.py": "", "pkg/m.py": "def norm_a(x):\n    y = x + 1\n    return y\n\n\ndef norm_b(x):\n    y = x + 1\n    return y\n"}
TWINS_RENAMED = {"pkg/__init__.py": "", "pkg/m.py": "def scale_a(x):\n    y = x + 1\n    return y\n\n\ndef scale_b(x):\n    y = x + 1\n    return y\n"}


def _write(root: Path, files: dict) -> None:
    for rel, src in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(src)


def _git(repo: Path, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def twins_repo(tmp_path):
    """A git repo whose second commit renames two body-identical helpers -> one identity_ambiguous event."""
    repo = tmp_path / "repo"
    _write(repo, TWINS)
    _git(repo, "init", "-q"); _git(repo, "add", "."); _git(repo, "commit", "-qm", "twins")
    return repo


def _run_twice(repo: Path, db: Path, env: dict | None = None, monkeypatch=None):
    if monkeypatch is not None:
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
    cli.run(str(repo), "amb", str(db), build_cards=False)
    _write(repo, TWINS_RENAMED); _git(repo, "commit", "-qam", "rename twins")
    cli.run(str(repo), "amb", str(db), build_cards=False)
    return sqlite3.connect(str(db))


def _events(conn, et):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM node_event WHERE event_type=? ORDER BY id", (et,))]


def _latest_ext(conn):
    row = conn.execute("SELECT extensions_json FROM analysis_run ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row and row[0] else None


def test_export_format_with_context(twins_repo, tmp_path):
    conn = _run_twice(twins_repo, tmp_path / "g.db")
    assert len(_events(conn, "identity_ambiguous")) == 1
    out = tmp_path / "calib.json"
    doc = calibration_export.export(conn, out, repo=str(twins_repo))
    assert json.loads(out.read_text()) == doc and doc["version"] == 1 and doc["source"]["events"] == 1
    it = doc["items"][0]
    assert it["truth"] is None and it["labelled_by"] is None and it["id"].startswith("e")
    amb = it["ambiguity"]
    assert amb["layer"] == "struct_sig" and amb["prev_run_id"] < amb["run_id"] and amb["commit_sha"] and amb["prev_commit_sha"]
    assert sorted(r["qualified_name"] for r in amb["prev"]) == ["pkg.m.norm_a", "pkg.m.norm_b"]
    assert sorted(r["qualified_name"] for r in amb["cur"]) == ["pkg.m.scale_a", "pkg.m.scale_b"]
    for r in amb["prev"] + amb["cur"]:
        assert r["file_path"] == "pkg/m.py" and r["line_start"] and r["struct_sig"] and r["dataflow_sig"] is not None
        assert r["context"] and ("def norm" in r["context"] or "def scale" in r["context"])
    assert all(r["node_key"].startswith("nk_") for r in amb["prev"])
    assert "def norm_a" in amb["prev"][0]["context"] or "def norm_b" in amb["prev"][0]["context"]   # read from the OLD commit
    # the export round-trips into the matcher's own types
    a = cal.ambiguity_from_item(it)
    assert {r.qualified_name for r in a.prev} == {"pkg.m.norm_a", "pkg.m.norm_b"}
    # and the CLI writes the same file
    assert cli.main(["ambiguities", str(tmp_path / "g.db"), "--export", str(tmp_path / "c2.json"), "--repo", str(twins_repo)]) == 0
    assert json.loads((tmp_path / "c2.json").read_text())["items"][0]["ambiguity"]["prev"] == amb["prev"]


def test_heuristic_is_consistent_on_the_exported_ambiguity(twins_repo, tmp_path, monkeypatch):
    conn = _run_twice(twins_repo, tmp_path / "g.db")
    out = tmp_path / "calib.json"
    calibration_export.export(conn, out, repo=str(twins_repo))
    monkeypatch.setenv("PROVLEDGER_ARBITER_EVAL_DIR", str(tmp_path / "eval"))
    rep = cal.run(cal.load_arbiter(HEUR), out, n_runs=3)
    assert rep.consistency == 1.0 and rep.coverage == 1.0 and rep.evidence_ok and rep.accuracy is None and rep.n_truth == 0
    assert rep.items[0]["answer"] == [["pkg.m.norm_a", "pkg.m.scale_a"], ["pkg.m.norm_b", "pkg.m.scale_b"]] or \
        rep.items[0]["answer"] == [("pkg.m.norm_a", "pkg.m.scale_a"), ("pkg.m.norm_b", "pkg.m.scale_b")]
    assert (tmp_path / "eval" / "provledger.heuristic.json").exists()
    assert cli.main(["arbiter-eval", str(out), "--arbiter", HEUR, "--n-runs", "2"]) == 0


def _synthetic_calib(path: Path, n=10):
    items = []
    for i in range(n):
        a, b = f"pkg.m.h{i}_a", f"pkg.m.h{i}_b"
        row = lambda qn, key="": {"qualified_name": qn, "node_key": key, "node_type": "function", "file_path": "pkg/m.py",
                                  "line_start": 1, "line_end": 2, "struct_sig": "s", "dataflow_sig": "d"}
        items.append({"id": f"s{i}", "ambiguity": {"layer": "struct_sig", "prev": [row(a, f"nk_a{i}"), row(b, f"nk_b{i}")],
                                                    "cur": [row(a + "2"), row(b + "2")]},
                      "truth": {"pairs": [[a, a + "2"], [b, b + "2"]]}, "labelled_by": "test"})
    path.write_text(json.dumps({"version": 1, "items": items}))
    return path


def test_gate_refuses_without_a_report_and_records_it(twins_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("PROVLEDGER_ARBITER_EVAL_DIR", str(tmp_path / "eval"))
    monkeypatch.setenv("PROVLEDGER_ARBITER_CALIB", str(_synthetic_calib(tmp_path / "calib.json")))
    conn = _run_twice(twins_repo, tmp_path / "g.db", {"PROVLEDGER_ARBITER": HEUR}, monkeypatch)
    ext = _latest_ext(conn)
    assert ext["arbiter"]["id"] == "provledger.heuristic" and ext["arbiter"]["gate"].startswith("refused: no evaluation report")
    assert _events(conn, "identity_ambiguous") and not _events(conn, "identity_asserted")     # not wired
    import selfcheck
    checks = {c["name"]: c for c in selfcheck.run(str(tmp_path / "g.db"))["checks"]}
    assert checks["arbiter_gate"]["ok"] is False and checks["arbiter_gate"]["severity"] == "warning" and "refused" in checks["arbiter_gate"]["detail"]


@pytest.mark.parametrize("spoil, expect", [
    ("consistency", "consistency"), ("accuracy", "accuracy"), ("few", "labelled item"), ("sha", "sha"),
])
def test_gate_refusals(twins_repo, tmp_path, monkeypatch, spoil, expect):
    ed = tmp_path / "eval"
    calib = _synthetic_calib(tmp_path / "calib.json")
    monkeypatch.setenv("PROVLEDGER_ARBITER_EVAL_DIR", str(ed))
    monkeypatch.setenv("PROVLEDGER_ARBITER_CALIB", str(calib))
    rep = cal.run(cal.load_arbiter(HEUR), calib, n_runs=2, out_dir=ed)
    assert rep.ok
    p = ed / "provledger.heuristic.json"
    d = json.loads(p.read_text())
    if spoil == "consistency":
        d["consistency"] = 0.9
    elif spoil == "accuracy":
        d["accuracy"] = 0.8
    elif spoil == "few":
        d["n_truth"] = 9
    elif spoil == "sha":
        calib.write_text(calib.read_text() + "\n")
    p.write_text(json.dumps(d))
    conn = _run_twice(twins_repo, tmp_path / "g.db", {"PROVLEDGER_ARBITER": HEUR}, monkeypatch)
    gate = _latest_ext(conn)["arbiter"]["gate"]
    assert gate.startswith("refused") and expect in gate, gate
    assert not _events(conn, "identity_asserted")


def test_gate_passes_and_wires_the_arbiter(twins_repo, tmp_path, monkeypatch):
    ed = tmp_path / "eval"
    calib = _synthetic_calib(tmp_path / "calib.json")
    monkeypatch.setenv("PROVLEDGER_ARBITER_EVAL_DIR", str(ed))
    monkeypatch.setenv("PROVLEDGER_ARBITER_CALIB", str(calib))
    assert cal.run(cal.load_arbiter(HEUR), calib, n_runs=3, out_dir=ed).ok
    conn = _run_twice(twins_repo, tmp_path / "g.db", {"PROVLEDGER_ARBITER": HEUR}, monkeypatch)
    ext = _latest_ext(conn)
    assert ext["arbiter"]["id"] == "provledger.heuristic" and ext["arbiter"]["gate"].startswith("passed")
    asserted = conn.execute("SELECT node_key, tier, payload_json FROM node_event WHERE event_type='identity_asserted' ORDER BY id").fetchall()
    assert len(asserted) == 2 and all(r[1] == "asserted" for r in asserted)
    payloads = [json.loads(r[2]) for r in asserted]
    assert {p["cur"] for p in payloads} == {"pkg.m.scale_a", "pkg.m.scale_b"} and all(p["arbiter"] == "provledger.heuristic" and p["evidence"] for p in payloads)
    prev_keys = {r[0] for r in conn.execute("SELECT node_key FROM node_snapshot WHERE run_id=1 AND qualified_name LIKE 'pkg.m.norm_%'")}
    assert {r[0] for r in asserted} == prev_keys
    import selfcheck
    checks = {c["name"]: c for c in selfcheck.run(str(tmp_path / "g.db"))["checks"]}
    assert checks["arbiter_gate"]["ok"] is True


def test_unset_env_means_no_arbiter_and_no_record(twins_repo, tmp_path, monkeypatch):
    monkeypatch.delenv("PROVLEDGER_ARBITER", raising=False)
    conn = _run_twice(twins_repo, tmp_path / "g.db")
    ext = _latest_ext(conn)
    assert ext is None or "arbiter" not in ext
    assert not _events(conn, "identity_asserted")


def test_gate_binds_the_repo_to_an_arbiter_that_asks(twins_repo, tmp_path, monkeypatch):
    """Phase 8: an arbiter exposing bind_repo(repo) is told where the working
    tree is, so it can read context lines for live rows (ClaudeArbiter does)."""
    import textwrap
    mod = tmp_path / "bindarb.py"
    mod.write_text(textwrap.dedent('''
        from provledger.testing.heuristic_arbiter import HeuristicArbiter
        class BindingArbiter(HeuristicArbiter):
            arbiter_id = "provledger.heuristic"
            bound = None
            def bind_repo(self, repo):
                BindingArbiter.bound = repo
        '''))
    monkeypatch.syspath_prepend(str(tmp_path))
    ed = tmp_path / "eval"
    calib = _synthetic_calib(tmp_path / "calib.json")
    monkeypatch.setenv("PROVLEDGER_ARBITER_EVAL_DIR", str(ed))
    monkeypatch.setenv("PROVLEDGER_ARBITER_CALIB", str(calib))
    assert cal.run(cal.load_arbiter(HEUR), calib, n_runs=3, out_dir=ed).ok
    conn = _run_twice(twins_repo, tmp_path / "g.db", {"PROVLEDGER_ARBITER": "bindarb:BindingArbiter"}, monkeypatch)
    import bindarb
    assert bindarb.BindingArbiter.bound == str(twins_repo)
    assert conn.execute("SELECT COUNT(*) FROM node_event WHERE event_type='identity_asserted'").fetchone()[0] == 2
