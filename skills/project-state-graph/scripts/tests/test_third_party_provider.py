"""Phase 6 Task 4: a third-party provider takes part in the history layer like
a built-in — its snapshots carry its type_id, they match by qualname across
runs — and every run records the provider set (with degradations)."""
import json
import sqlite3
import textwrap

import selfcheck
from analyzer import _host, cli, history, namesets, store
from tests.test_history import conn  # noqa: F401

providers = _host.providers
g = _host.graph_api

SRC = {"pkg/__init__.py": "", "pkg/m.py": "# @dataset: orders\n# @dataset: customers\n\ndef load():\n    return 1\n"}
PROVIDER_MOD = '''
import os
from provledger.graph_api import MUTATIONS, NodeObservation, Signature

class DatasetComments:
    """`# @dataset: name` comments become dataset nodes."""
    type_id = "acme.example"
    schema_version = 1
    requires = ()
    def extract(self, ctx):
        out = []
        for rel in sorted(ctx.file_map):
            if not rel.endswith(".py"):
                continue
            mod = rel[:-3].replace("/", ".")
            for n, line in enumerate(open(os.path.join(ctx.repo_root, rel), encoding="utf-8"), 1):
                if line.startswith("# @dataset:"):
                    name = line.split(":", 1)[1].strip()
                    qn = f"{mod}:{name}"
                    out.append(NodeObservation(type_id=self.type_id, node_type="dataset", qualified_name=qn,
                                               file_path=rel, line_start=n, line_end=n,
                                               signatures=(Signature("qualname", qn),), attrs={"source": "comment"}))
        return out
    def attributes_schema(self):
        return {"required": ["source"], "types": {"source": "str"}}
    def declared_stability(self):
        return {m: "preserved" for m in MUTATIONS}
'''


def _write(tmp_path, files):
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)


def _run(conn, repo, plist):
    from analyzer import py_ast, walker
    run_id = store.start_run(conn, project_name="demo", commit_sha="c")
    store.reset_graph(conn)
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    report = {}
    history.snapshot_run(conn, str(repo), run_id, providers=plist, report=report, file_map=fm)
    history.resolve(conn, run_id)
    store.stamp_run(conn, run_id)
    store.finish_run(conn, run_id)
    return run_id, report


def test_third_party_provider_snapshots_and_matches_by_qualname(conn, tmp_path, monkeypatch):
    (tmp_path / "acme_provider.py").write_text(textwrap.dedent(PROVIDER_MOD))
    monkeypatch.syspath_prepend(str(tmp_path))
    repo = tmp_path / "repo"
    _write(repo, SRC)
    (repo / "provledger-extensions.json").write_text(json.dumps(
        {"version": 1, "providers": [{"id": "acme.example", "module": "acme_provider:DatasetComments", "priority": 3}]}))
    plist, records = providers.load_providers(_host.extensions.current(str(repo)))
    assert [p.type_id for p in plist] == ["acme.example", "provledger.symbol", "provledger.owned"]
    r1, rep1 = _run(conn, repo, plist)
    rows = conn.execute("SELECT qualified_name, attrs_json FROM node_snapshot WHERE run_id=? AND node_type='dataset'", (r1,)).fetchall()
    assert {q for q, _ in rows} == {"pkg.m:orders", "pkg.m:customers"}
    assert all(json.loads(a)["type_id"] == "acme.example" for _, a in rows)
    assert rep1["acme.example"] == {"degraded": None, "observations": 2, "elapsed_s": rep1["acme.example"]["elapsed_s"], "schema_version": 1}
    r2, _ = _run(conn, repo, plist)
    ev = conn.execute("SELECT event_type, node_key FROM node_event WHERE run_id=? ORDER BY seq", (r2,)).fetchall()
    keys1 = {k for k, in conn.execute("SELECT node_key FROM node_snapshot WHERE run_id=? AND node_type='dataset'", (r1,))}
    assert {k for t, k in ev if t == "node_matched"} >= keys1 and not [t for t, _ in ev if t == "node_added"]


def test_cli_records_the_provider_set_and_selfcheck_warns_on_degradation(tmp_path, monkeypatch, capsys):
    (tmp_path / "acme_provider.py").write_text(textwrap.dedent(PROVIDER_MOD))
    monkeypatch.syspath_prepend(str(tmp_path))
    repo = tmp_path / "repo"
    _write(repo, SRC)
    (repo / "provledger-extensions.json").write_text(json.dumps({"version": 1, "providers": [
        {"id": "acme.example", "module": "acme_provider:DatasetComments"},
        {"id": "acme.missing", "module": "no_such_module:Thing"}]}))
    namesets.reset()
    db = tmp_path / "g.db"
    cli.run(str(repo), "demo", str(db))
    namesets.reset()
    c = sqlite3.connect(str(db))
    fp = json.loads(c.execute("SELECT extensions_json FROM analysis_run ORDER BY id DESC LIMIT 1").fetchone()[0])
    c.close()
    by_id = {p["id"]: p for p in fp["providers"]}
    assert set(by_id) == {"acme.example", "acme.missing", "provledger.symbol", "provledger.owned"}
    assert by_id["acme.example"]["observations"] == 2 and by_id["acme.example"]["degraded"] is None
    assert by_id["acme.missing"]["degraded"] and by_id["acme.missing"]["observations"] == 0
    assert set(by_id["provledger.symbol"]) >= {"id", "module", "schema_version", "enabled", "priority", "degraded", "observations", "elapsed_s"}
    res = selfcheck.run(str(db))
    chk = next(x for x in res["checks"] if x["name"] == "providers_degraded")
    assert chk["ok"] is False and chk["severity"] == "warning" and "acme.missing" in chk["detail"]
    err = capsys.readouterr().err
    assert "WARNING: provider acme.missing degraded" in err and "import failed" in err     # never silent on stdout/stderr
