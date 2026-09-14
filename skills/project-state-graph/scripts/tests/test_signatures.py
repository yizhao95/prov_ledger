"""analyzer.signatures — the deterministic identity signatures of spec §2.3."""
import ast
from pathlib import Path

from analyzer import signatures as sg


def _fn(src):
    return ast.parse(src).body[0]


def test_struct_sig_invariant_to_names_comments_docstring_format():
    a = _fn('def f(df):\n    """doc"""\n    # c\n    x = df[df.q > 0]\n    return x\n')
    b = _fn('def g(frame):\n    y = frame[frame.q > 0]\n    return y\n')
    c = _fn('def f(df):\n    x = df[df.q>0]; return x\n')
    assert sg.struct_sig(a) == sg.struct_sig(b) == sg.struct_sig(c)


def test_struct_sig_changes_on_statement_change():
    a = _fn('def f(df):\n    return df[df.q > 0]\n')
    b = _fn('def f(df):\n    return df[df.q > 1]\n')
    assert sg.struct_sig(a) != sg.struct_sig(b)


def test_struct_sig_globals_not_renamed():
    a = _fn('def f(df):\n    return helper(df)\n')
    b = _fn('def f(df):\n    return other(df)\n')
    assert sg.struct_sig(a) != sg.struct_sig(b)


def test_struct_sig_ignores_annotations_and_class_name():
    a = _fn('class Trainer:\n    def fit(self, X: pd.DataFrame) -> "Trainer":\n        self.m = X\n        return self\n')
    b = _fn('class Estimator:\n    def fit(self, feats) -> "Estimator":\n        self.m = feats\n        return self\n')
    assert sg.struct_sig(a) == sg.struct_sig(b)                    # class node: nested method params alpha-normalised
    assert sg.struct_sig(a.body[0]) == sg.struct_sig(b.body[0])    # method node


def test_struct_sig_is_stable_across_processes_and_calls():
    a = _fn('def f(x):\n    return x + 1\n')
    assert sg.struct_sig(a) == sg.struct_sig(_fn('def f(x):\n    return x + 1\n'))
    assert len(sg.struct_sig(a)) == 16


# ── corpus guard (phase-1 review #6) ─────────────────────────────────────────

def _all_struct_sigs(repo: Path) -> dict:
    """{qualified_name: struct_sig} for every top-level def/class and method,
    using the corpus' module-relative qualified-name rule."""
    out = {}
    for py in sorted(repo.rglob("*.py")):
        mod = ".".join(py.relative_to(repo).with_suffix("").parts)
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        for node in ast.parse(py.read_text()).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out[f"{mod}.{node.name}"] = sg.struct_sig(node)
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            out[f"{mod}.{node.name}.{item.name}"] = sg.struct_sig(item)
    return out


def test_corpus_struct_sig_symbols_are_alpha_equal():
    """Every via="struct_sig" symbol must have an alpha-equal body on the other
    side, and swap_two_similar's pair must be equal on the before side — so the
    corpus and signatures.py cannot drift apart."""
    from tests.corpus.harness import iter_cases
    cases = iter_cases()                       # phase 6: the package corpus
    assert cases
    checked = 0
    for case in cases:
        for name, spec in case.expect["variants"].items():
            vdir = case.variants[name]
            if spec.get("via") == "struct_sig":
                base_dir = vdir / "before" if (vdir / "before").exists() else case.base
                other_dir = vdir / "after" if (vdir / "after").exists() else vdir
            elif spec.get("identity") == "ambiguous":
                base_dir, other_dir = vdir / "before", None
            else:
                continue
            sigs = _all_struct_sigs(base_dir)
            syms = spec.get("symbols", case.expect["case"]["symbols"])
            if other_dir is not None:
                other = _all_struct_sigs(other_dir)
                for s in syms:
                    assert sigs[s] in other.values(), f"{case.name}/{name}: {s} has no alpha-equal body on the other side"
            else:
                assert len({sigs[s] for s in syms}) == 1, f"{case.name}/{name}: swap pair bodies differ"
            checked += 1
    assert checked >= 6


# ── dataflow_sig from edges ──────────────────────────────────────────────────

def test_dataflow_sig_from_edges(tmp_path):
    from analyzer import dataflow, dataflow_types, py_ast, sql_refs, store, walker
    repo = tmp_path / "repo"; (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text(
        'import pandas as pd\ndef load(path: str) -> pd.DataFrame:\n    return pd.read_sql("SELECT a, b FROM t", None)\n'
        'def use(df: pd.DataFrame) -> int:\n    return len(load("x"))\n')
    conn = store.init_db(str(tmp_path / "x-state-graph.db"))
    fm = walker.walk(conn, str(repo)); py_ast.analyze(conn, str(repo), fm)
    dataflow.analyze(conn, str(repo), fm); dataflow_types.analyze(conn, str(repo), fm)
    sql_refs.analyze(conn, str(repo), fm)
    nid = conn.execute("SELECT id FROM node WHERE qualified_name='pkg.m.load'").fetchone()[0]
    sig, trivial, attrs = sg.dataflow_sig(conn, nid)
    assert not trivial and attrs["reads"] == ["t"] and attrs["return"] != "unknown"
    assert len(sig) == 16 and sig == sg.dataflow_sig(conn, nid)[0]
    nid2 = conn.execute("SELECT id FROM node WHERE qualified_name='pkg.m.use'").fetchone()[0]
    assert "load" in sg.dataflow_sig(conn, nid2)[2]["callees"]


def test_dataflow_sig_trivial_when_nothing_known(tmp_path):
    from analyzer import py_ast, store, walker
    repo = tmp_path / "repo"; (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text('def f(x):\n    return x\n')
    conn = store.init_db(str(tmp_path / "x-state-graph.db"))
    fm = walker.walk(conn, str(repo)); py_ast.analyze(conn, str(repo), fm)
    nid = conn.execute("SELECT id FROM node WHERE qualified_name='pkg.m.f'").fetchone()[0]
    assert sg.dataflow_sig(conn, nid)[1] is True
