"""Phase 6 Task 3: the two built-in reference providers pass the conformance
suite with the analyzer-backed context (a real graph per corpus repository)."""
import sqlite3
import tempfile
from pathlib import Path

from analyzer import _host
from tests.corpus.harness import _harness  # noqa: F401  (the shell; ensures the package corpus is importable)

providers = _host.providers
conformance = __import__(f"{_host.testing.__name__}.conformance", fromlist=["x"])
builtin_symbols = __import__(f"{providers.__name__}.builtin_symbols", fromlist=["x"])
builtin_owned = __import__(f"{providers.__name__}.builtin_owned", fromlist=["x"])


def analyzer_context(repo: Path):
    """Build a throw-away graph of `repo` and hand the provider a read-only view of it."""
    from analyzer import api_refs, data_model, dataflow, dataflow_types, py_ast, sql_refs, store, walker
    d = tempfile.mkdtemp(prefix="conf-")
    db = str(Path(d) / "g.db")
    conn = store.init_db(db)
    run_id = store.start_run(conn, project_name="conformance")
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    data_model.analyze(conn, str(repo), fm)
    sql_refs.analyze(conn, str(repo), fm)
    api_refs.analyze(conn, str(repo), fm)
    conn.commit()
    conn.close()
    return providers.make_context(db, str(repo), run_id, fm)


def test_builtin_symbol_provider_passes_conformance():
    rep = conformance.run(builtin_symbols.BuiltinSymbolProvider(), context_factory=analyzer_context)
    assert rep.ok is True, rep.text()


def test_builtin_owned_provider_passes_conformance_with_its_owners():
    rep = conformance.run(builtin_owned.BuiltinOwnedProvider(), context_factory=analyzer_context,
                          companions=[builtin_symbols.BuiltinSymbolProvider()])
    assert rep.ok is True, rep.text()
