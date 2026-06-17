"""RED tests for analyzer.sql_refs — sql_table/bq_dataset nodes + reads/writes edges."""
import pytest

from analyzer import py_ast, sql_refs, store, walker

PY_SAMPLE = '''\
PROJECT = "wmt-proj"


def build_query(store_id):
    return f"SELECT a, b FROM `wmt-proj.retail.events` WHERE store = {store_id}"


def write_results():
    return "INSERT INTO `wmt-proj.retail.summary` SELECT * FROM `wmt-proj.retail.events`"
'''

SQL_SAMPLE = "SELECT x FROM sales JOIN inventory ON sales.id = inventory.id\n"


@pytest.fixture
def analyzed(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "q.py").write_text(PY_SAMPLE)
    (repo / "report.sql").write_text(SQL_SAMPLE)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    file_map = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), file_map)
    sql_refs.analyze(conn, str(repo), file_map)
    yield conn
    conn.close()


def _node_names(conn, type_name):
    return {
        r[0]
        for r in conn.execute(
            """SELECT n.name FROM node n JOIN node_type t ON n.node_type_id=t.id
               WHERE t.name=?""",
            (type_name,),
        ).fetchall()
    }


def _edges(conn, type_name):
    return conn.execute(
        """SELECT s.name, d.name
           FROM edge e
           JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node s ON e.src_node_id=s.id
           JOIN node d ON e.dst_node_id=d.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


def test_sql_file_tables_become_nodes(analyzed):
    tables = _node_names(analyzed, "sql_table")
    assert "sales" in tables
    assert "inventory" in tables


def test_bq_dataset_nodes_from_python_strings(analyzed):
    bq = _node_names(analyzed, "bq_dataset")
    assert "wmt-proj.retail.events" in bq


def test_reads_sql_edge_from_enclosing_function(analyzed):
    reads = {(s, d) for s, d in _edges(analyzed, "reads_sql")}
    assert ("build_query", "wmt-proj.retail.events") in reads


def test_writes_sql_edge_for_insert_target(analyzed):
    writes = {(s, d) for s, d in _edges(analyzed, "writes_sql")}
    assert ("write_results", "wmt-proj.retail.summary") in writes
