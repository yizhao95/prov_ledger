"""Reproducibility (phase 5 Task 4): every analysis_run records the extension
set behind it — analysis_run.extensions_json = extensions.fingerprint() or
NULL without a file — and the history CLI shows ext=<sha[:8]> per event."""
import io
import json
import sqlite3
from contextlib import redirect_stdout

from analyzer import cli, namesets

SRC = "def go(data):\n    X_train, X_test = stratified_split(data)\n    return X_train\n"
EXT = {"version": 1,
       "drift_kinds": [{"id": "acme.null_up", "metric": "null_frac", "op": "delta_gte", "value": 0.1, "priority": 2}],
       "namesets": [{"set": "split_funcs", "add": ["stratified_split"]}]}


def _repo(tmp_path, name, ext=None):
    repo = tmp_path / name
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(SRC)
    if ext is not None:
        (repo / "provledger-extensions.json").write_text(json.dumps(ext))
    return repo


def _runs(db):
    c = sqlite3.connect(str(db))
    rows = c.execute("SELECT id, extensions_json FROM analysis_run ORDER BY id").fetchall()
    c.close()
    return rows


def test_run_records_fingerprint_or_null(tmp_path):
    namesets.reset()
    with_ext = _repo(tmp_path, "a", EXT)
    without = _repo(tmp_path, "b")
    db_a, db_b = tmp_path / "a.db", tmp_path / "b.db"
    cli.run(str(with_ext), "a", str(db_a))
    cli.run(str(without), "b", str(db_b))
    (_, fp_json), = _runs(db_a)
    fp = json.loads(fp_json)
    assert fp["path"].endswith("provledger-extensions.json") and len(fp["sha256"]) == 64
    assert fp["drift_kinds"] == ["acme.null_up"] and fp["namesets"] == {"split_funcs": ["stratified_split"]}
    assert fp["constraints"] == 0
    (_, none), = _runs(db_b)
    assert none is None
    namesets.reset()


def test_two_runs_over_the_same_file_share_the_sha(tmp_path):
    namesets.reset()
    repo = _repo(tmp_path, "a", EXT)
    db = tmp_path / "a.db"
    cli.run(str(repo), "a", str(db))
    cli.run(str(repo), "a", str(db))
    rows = _runs(db)
    assert len(rows) == 2 and json.loads(rows[0][1])["sha256"] == json.loads(rows[1][1])["sha256"]
    namesets.reset()


def test_history_cli_shows_the_extension_sha(tmp_path):
    namesets.reset()
    repo = _repo(tmp_path, "a", EXT)
    db = tmp_path / "a.db"
    cli.run(str(repo), "a", str(db))
    (_, fp_json), = _runs(db)
    sha8 = json.loads(fp_json)["sha256"][:8]
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.history_main([str(db), "pkg.mod.go"])
    assert f"ext={sha8}" in buf.getvalue()
    repo_b = _repo(tmp_path, "b")
    db_b = tmp_path / "b.db"
    cli.run(str(repo_b), "b", str(db_b))
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.history_main([str(db_b), "pkg.mod.go"])
    assert "ext=none" in buf.getvalue()
    namesets.reset()
