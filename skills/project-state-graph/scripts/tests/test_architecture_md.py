"""RED tests for architecture_md — shallow-layer ARCHITECTURE.md generator."""
import architecture_md
from analyzer import (
    store, walker, py_ast, dataflow, dataflow_types,
    sql_refs, pipeline, cards,
)


def _build_db(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "lib").mkdir(parents=True)
    (repo / "app" / "main.py").write_text(
        "def index():\n    return helper()\n\n"
        "def helper():\n    return 1\n"
    )
    (repo / "lib" / "util.py").write_text(
        "def shared():\n    return 2\n"
    )
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    run_id = store.start_run(conn, project_name="demo", commit_sha="abc123")
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    sql_refs.analyze(conn, str(repo), fm)
    pipeline.analyze(conn, str(repo), fm)
    cards.build_symbol_cards(conn)
    store.finish_run(conn, run_id)
    conn.commit()
    conn.close()
    return path


def test_generate_has_summary_header(tmp_path):
    path = _build_db(tmp_path)
    md = architecture_md.generate(path, repo_name="demo")
    assert "# demo" in md or "demo" in md.splitlines()[0]
    assert "Architecture" in md


def test_generate_lists_every_file(tmp_path):
    path = _build_db(tmp_path)
    md = architecture_md.generate(path, repo_name="demo")
    assert "app/main.py" in md
    assert "lib/util.py" in md


def test_generate_groups_by_subsystem(tmp_path):
    path = _build_db(tmp_path)
    md = architecture_md.generate(path, repo_name="demo")
    # top-level dirs become subsystem groups
    assert "app" in md
    assert "lib" in md


def test_generate_mentions_known_symbol(tmp_path):
    path = _build_db(tmp_path)
    md = architecture_md.generate(path, repo_name="demo")
    assert "index" in md
    assert "helper" in md


def test_write_file_creates_architecture_md(tmp_path):
    path = _build_db(tmp_path)
    out = tmp_path / "ARCHITECTURE.md"
    architecture_md.write_file(path, repo_name="demo", out_path=str(out))
    assert out.exists()
    assert "app/main.py" in out.read_text()
