"""Phase 5.1 — the generated viz must not be XSS-injectable (PSG-S1)."""
from analyzer import graph_viz, store


def test_safe_json_escapes_script_breakout():
    out = graph_viz._safe_json({"name": "evil</script><img src=x onerror=alert(1)>"})
    assert "</script>" not in out
    assert "\\u003c/script\\u003e" in out


def test_write_html_escapes_title_and_node_names(tmp_path):
    db = str(tmp_path / "g.db")
    conn = store.init_db(db)
    ft = store.get_or_create_node_type(conn, "function")
    fl = store.get_or_create_node_type(conn, "file")
    store.add_node(conn, fl, name="m.py", qualified_name="m.py", file_path="m.py")
    store.add_node(conn, ft, name="evil</script>fn", qualified_name="m.evil",
                   file_path="m.py")
    conn.commit()
    conn.close()

    out_path = str(tmp_path / "viz.html")
    graph_viz.write_html(db, out_path, level="full", title="<script>alert(1)</script>")
    html = open(out_path, encoding="utf-8").read()

    # the injected TITLE is HTML-escaped, not a live tag
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # a node name containing </script> is escaped in the embedded JSON data
    assert "evil</script>fn" not in html
    assert "evil\\u003c/script\\u003efn" in html
