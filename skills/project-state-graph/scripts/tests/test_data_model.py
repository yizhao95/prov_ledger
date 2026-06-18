"""RED tests for analyzer.data_model — DataFrame / column / dataset modeling.

Models data as first-class nodes:
  dataframe : a pandas/pyspark frame bound to a variable
  column    : a named field accessed via df["col"], with dtype + provenance
  dataset   : a declared schema (StructType / pandera / pydantic) source

Edges:
  has_column : dataframe -> column
  derives    : dataframe -> dataframe (op-labeled, e.g. df2 = df.filter(...))
  transforms : column -> column (e.g. df["c"] = df["a"] + df["b"])
"""
import json

import pytest

from analyzer import data_model, py_ast, store, walker


def _analyze(tmp_path, source: str):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(source)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    data_model.analyze(conn, str(repo), fm)
    return conn


def _nodes(conn, type_name):
    return conn.execute(
        """SELECT n.id, n.name, n.metadata_json
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


def _edges(conn, type_name):
    return conn.execute(
        """SELECT s.name, d.name, e.metadata_json
           FROM edge e
           JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node s ON e.src_node_id=s.id
           JOIN node d ON e.dst_node_id=d.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


def test_dataframe_node_from_pandas_constructor(tmp_path):
    conn = _analyze(
        tmp_path,
        "import pandas as pd\n\n\ndef f():\n    df = pd.DataFrame({'a': [1]})\n    return df\n",
    )
    names = {r[1] for r in _nodes(conn, "dataframe")}
    assert "df" in names


def test_column_node_from_subscript(tmp_path):
    conn = _analyze(
        tmp_path,
        "import pandas as pd\n\n\ndef f():\n    df = pd.DataFrame({'a': [1]})\n    return df['a']\n",
    )
    cols = {r[1] for r in _nodes(conn, "column")}
    assert "a" in cols


def test_has_column_edge(tmp_path):
    conn = _analyze(
        tmp_path,
        "import pandas as pd\n\n\ndef f():\n    df = pd.DataFrame({'a': [1]})\n    x = df['a']\n    return x\n",
    )
    edges = _edges(conn, "has_column")
    assert any(s == "df" and d == "a" for s, d, _ in edges)


def test_astype_sets_column_dtype(tmp_path):
    conn = _analyze(
        tmp_path,
        "import pandas as pd\n\n\ndef f():\n    df = pd.DataFrame({'a': [1]})\n    df['a'] = df['a'].astype('int64')\n    return df\n",
    )
    cols = _nodes(conn, "column")
    dtypes = {json.loads(m or "{}").get("dtype") for _, n, m in cols if n == "a"}
    assert "int64" in dtypes


def test_dtype_provenance_recorded(tmp_path):
    conn = _analyze(
        tmp_path,
        "import pandas as pd\n\n\ndef f():\n    df = pd.DataFrame({'a': [1]})\n    df['a'] = df['a'].astype('int64')\n    return df\n",
    )
    cols = _nodes(conn, "column")
    provs = {json.loads(m or "{}").get("dtype_provenance") for _, n, m in cols if n == "a"}
    # astype is a static declaration of dtype
    assert provs & {"static-inference", "declared-schema", "annotation"}


def test_struct_type_schema_creates_dataset_with_typed_columns(tmp_path):
    src = (
        "from pyspark.sql.types import StructType, StructField, StringType, IntegerType\n\n\n"
        "schema = StructType([\n"
        "    StructField('name', StringType()),\n"
        "    StructField('age', IntegerType()),\n"
        "])\n"
    )
    conn = _analyze(tmp_path, src)
    ds = _nodes(conn, "dataset")
    assert ds, "expected a dataset node from StructType schema"
    cols = {r[1] for r in _nodes(conn, "column")}
    assert {"name", "age"} <= cols
    # dtypes captured from the schema (declared)
    coldtypes = {
        r[1]: json.loads(r[2] or "{}").get("dtype") for r in _nodes(conn, "column")
    }
    assert coldtypes.get("name") in {"StringType", "string"}
    assert coldtypes.get("age") in {"IntegerType", "int", "integer"}


def test_derives_edge_between_dataframes(tmp_path):
    src = (
        "import pandas as pd\n\n\n"
        "def f():\n"
        "    df = pd.DataFrame({'a': [1]})\n"
        "    df2 = df.dropna()\n"
        "    return df2\n"
    )
    conn = _analyze(tmp_path, src)
    edges = _edges(conn, "derives")
    assert any(s == "df" and d == "df2" for s, d, _ in edges)
    ops = {json.loads(m or "{}").get("op") for _, _, m in edges}
    assert "dropna" in ops


def test_transforms_edge_column_to_column(tmp_path):
    src = (
        "import pandas as pd\n\n\n"
        "def f():\n"
        "    df = pd.DataFrame({'a': [1], 'b': [2]})\n"
        "    df['c'] = df['a'] + df['b']\n"
        "    return df\n"
    )
    conn = _analyze(tmp_path, src)
    edges = _edges(conn, "transforms")
    pairs = {(s, d) for s, d, _ in edges}
    assert ("a", "c") in pairs
    assert ("b", "c") in pairs
