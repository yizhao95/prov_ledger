"""Phase 2.1 — shared qualified-name resolver (PSG-C2 foundation)."""
from analyzer import _resolve, store


def _fn(conn, ft, name, qual):
    return store.add_node(conn, ft, name=name, qualified_name=qual)


def test_qualified_resolution_picks_the_right_module(tmp_path):
    conn = store.init_db(str(tmp_path / "g.db"))
    ft = store.get_or_create_node_type(conn, "function")
    m1_run = _fn(conn, ft, "run", "m1.run")
    m2_run = _fn(conn, ft, "run", "m2.run")
    m1_load = _fn(conn, ft, "load", "m1.load")
    idx = _resolve.build_index(conn)

    # module-qualified resolution of an ambiguous simple name -> the right node, high
    assert idx.resolve("run", module="m1") == [(m1_run, "high")]
    assert idx.resolve("run", module="m2") == [(m2_run, "high")]

    # no module + ambiguous -> ALL candidates, inferred (caller emits per-candidate)
    amb = idx.resolve("run")
    assert {nid for nid, _ in amb} == {m1_run, m2_run}
    assert all(conf == "inferred" for _, conf in amb)

    # unique simple name -> high
    assert idx.resolve("load") == [(m1_load, "high")]

    # unknown -> empty
    assert idx.resolve("nope") == []
    conn.close()


def test_class_qualified_method(tmp_path):
    conn = store.init_db(str(tmp_path / "g.db"))
    mt = store.get_or_create_node_type(conn, "method")
    m = _fn(conn, mt, "fit", "m1.Model.fit")
    idx = _resolve.build_index(conn)
    assert idx.resolve("fit", module="m1", klass="Model") == [(m, "high")]
    conn.close()


def test_call_name_helper():
    import ast
    tree = ast.parse("foo(); obj.bar()")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    names = {_resolve.call_name(c.func) for c in calls}
    assert names == {"foo", "bar"}
