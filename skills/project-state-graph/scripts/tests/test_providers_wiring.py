"""Phase 6 Task 3: the built-in identity types are NodeTypeProviders and
history.snapshot_run only knows providers (run through providers.run_provider)."""
import json
from pathlib import Path

from analyzer import _host, history, signatures, store
from tests.test_history import BASE, _full_run, conn  # noqa: F401

providers = _host.providers
graph_api = _host.graph_api
builtin_symbols = __import__(f"{providers.__name__}.builtin_symbols", fromlist=["x"])
builtin_owned = __import__(f"{providers.__name__}.builtin_owned", fromlist=["x"])

DF_SRC = {"pkg/__init__.py": "", "pkg/m.py": ("import pandas as pd\n\n"
          "def load(path):\n    df = pd.read_csv(path)\n    df['qty'] = df['qty'].astype(int)\n    return df\n\n"
          "def main():\n    return load('p')\n")}


def _snap(conn, run_id):
    return conn.execute("SELECT node_type, qualified_name, attrs_json FROM node_snapshot WHERE run_id=? ORDER BY id", (run_id,)).fetchall()


def test_builtin_providers_are_the_default_and_carry_type_ids(conn, tmp_path):
    r1 = _full_run(conn, tmp_path, DF_SRC)
    rows = _snap(conn, r1)
    kinds = {(t, json.loads(a)["type_id"]) for t, _, a in rows}
    assert ("function", "provledger.symbol") in kinds and ("dataframe", "provledger.owned") in kinds
    assert all(json.loads(a).get("schema_version") == 1 for _, _, a in rows)
    ids = [p.type_id for p in providers.builtin_providers()]
    assert ids == ["provledger.symbol", "provledger.owned", "provledger.declared"]   # DP 2c added the third
    assert all(isinstance(p, graph_api.NodeTypeProvider) for p in providers.builtin_providers())


def test_snapshot_run_runs_the_given_providers_and_isolates_a_failure(conn, tmp_path):
    class Boom:
        type_id = "acme.boom"
        schema_version = 1
        requires = ()
        def extract(self, ctx):
            raise RuntimeError("provider exploded")
        def attributes_schema(self):
            return {}
        def declared_stability(self):
            return {m: "preserved" for m in graph_api.MUTATIONS}
    for rel, src in BASE.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    from analyzer import dataflow, dataflow_types, py_ast, walker
    run_id = store.start_run(conn, project_name="demo", commit_sha="c")
    fm = walker.walk(conn, str(tmp_path))
    py_ast.analyze(conn, str(tmp_path), fm)
    dataflow.analyze(conn, str(tmp_path), fm)
    dataflow_types.analyze(conn, str(tmp_path), fm)
    report = {}
    n = history.snapshot_run(conn, str(tmp_path), run_id, providers=[*providers.builtin_providers(), Boom()], report=report)
    assert n >= 3
    assert report["acme.boom"]["degraded"].startswith("RuntimeError") and report["acme.boom"]["observations"] == 0
    assert report["provledger.symbol"]["degraded"] is None and report["provledger.symbol"]["observations"] >= 3
    history.resolve(conn, run_id)
    assert {q for _, q, _ in _snap(conn, run_id)} >= {"pkg.m.load", "pkg.m.clean", "pkg.m.main"}


def test_signatures_are_the_package_reference_implementation():
    import ast
    fn = ast.parse("def f(a, b):\n    c = a + b\n    return c\n").body[0]
    assert signatures.struct_sig(fn) == builtin_symbols.struct_sig(fn)
    assert signatures.struct_sig is builtin_symbols.struct_sig


def test_snapshot_rows_match_the_pre_provider_shape(conn, tmp_path):
    """The reference implementation reproduces exactly what the inline code
    produced: same rows, same order, same signatures, same owner attrs."""
    r1 = _full_run(conn, tmp_path, DF_SRC)
    rows = _snap(conn, r1)
    qns = [q for _, q, _ in rows]
    assert qns.index("pkg.m.load") < qns.index("pkg.m.main")
    df_rows = [(q, json.loads(a)) for t, q, a in rows if t == "dataframe"]
    assert df_rows and all(a["owner_qn"] == "pkg.m.load" and a["name"] == "df" for _, a in df_rows)
    col = [(q, json.loads(a)) for t, q, a in rows if t == "column"]
    assert col and all(a["owner_qn"].startswith("pkg.m.load:df") for _, a in col)
    fn_rows = [r for r in rows if r[0] == "function"]
    assert all(json.loads(a)["node_id"] for _, _, a in fn_rows)
