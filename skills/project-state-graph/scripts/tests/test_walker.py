"""RED tests for analyzer.walker — file discovery + file nodes."""
import pytest

from analyzer import store, walker


@pytest.fixture
def conn(tmp_path):
    c = store.init_db(str(tmp_path / "demo-state-graph.db"))
    yield c
    c.close()


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "main.py").write_text("x = 1\n")
    (repo / "app" / "ui.js").write_text("const a = 1;\n")
    (repo / "app" / "page.html").write_text("<html></html>\n")
    (repo / "app" / "style.css").write_text("a{}\n")
    (repo / "q.sql").write_text("SELECT 1\n")
    # noise that must be ignored
    (repo / ".venv").mkdir()
    (repo / ".venv" / "junk.py").write_text("nope\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.js").write_text("nope\n")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "c.pyc").write_text("nope\n")
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref\n")
    # generated coverage/report artifacts that must be ignored
    (repo / "htmlcov").mkdir()
    (repo / "htmlcov" / "index.html").write_text("<html>cov</html>\n")
    (repo / "htmlcov" / "d_abc_main_py.html").write_text("<html>cov</html>\n")
    return repo


def _file_nodes(conn):
    return conn.execute(
        """SELECT n.file_path, n.metadata_json
           FROM node n JOIN node_type t ON n.node_type_id = t.id
           WHERE t.name = 'file'"""
    ).fetchall()


def test_discovers_supported_files(tmp_path, conn):
    repo = _make_repo(tmp_path)
    walker.walk(conn, str(repo))
    paths = {r[0] for r in _file_nodes(conn)}
    assert "app/main.py" in paths
    assert "app/ui.js" in paths
    assert "app/page.html" in paths
    assert "app/style.css" in paths
    assert "q.sql" in paths


def test_skips_ignored_dirs(tmp_path, conn):
    repo = _make_repo(tmp_path)
    walker.walk(conn, str(repo))
    paths = {r[0] for r in _file_nodes(conn)}
    assert not any(".venv" in p for p in paths)
    assert not any("node_modules" in p for p in paths)
    assert not any("__pycache__" in p for p in paths)
    assert not any(".git" in p for p in paths)
    assert not any("htmlcov" in p for p in paths)


def test_one_node_per_file(tmp_path, conn):
    repo = _make_repo(tmp_path)
    walker.walk(conn, str(repo))
    assert len(_file_nodes(conn)) == 5


def test_records_language_metadata(tmp_path, conn):
    import json
    repo = _make_repo(tmp_path)
    walker.walk(conn, str(repo))
    langs = {
        r[0]: json.loads(r[1])["language"]
        for r in _file_nodes(conn)
    }
    assert langs["app/main.py"] == "python"
    assert langs["app/ui.js"] == "javascript"
    assert langs["app/page.html"] == "html"
    assert langs["app/style.css"] == "css"
    assert langs["q.sql"] == "sql"


def test_walk_returns_path_to_node_id_map(tmp_path, conn):
    repo = _make_repo(tmp_path)
    result = walker.walk(conn, str(repo))
    assert isinstance(result, dict)
    assert "app/main.py" in result
    assert isinstance(result["app/main.py"], int)
