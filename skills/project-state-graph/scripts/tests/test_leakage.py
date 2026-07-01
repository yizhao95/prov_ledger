"""Phase 4.1 — data-leakage gate (north-star acceptance example A)."""
import selfcheck
from analyzer import leakage, py_ast, store, walker

LEAK_RAW = "def go(df, m):\n    m.fit(df)\n    m.score(df)\n"
CLEAN_SPLIT = (
    "def go(X, y, m):\n"
    "    Xtr, Xte, ytr, yte = train_test_split(X, y)\n"
    "    m.fit(Xtr)\n"
    "    m.predict(Xte)\n"
)
LEAK_SAME_SIDE = (
    "def go(X, m):\n"
    "    Xtr, Xte = train_test_split(X)\n"
    "    m.fit(Xtr)\n"
    "    m.predict(Xtr)\n"  # evaluating on the TRAIN split
)
CLEAN_DIFF_SOURCES = "def go(a, b, m):\n    m.fit(a)\n    m.predict(b)\n"


def test_detect_raw_dual_use():
    leaks = leakage.detect(LEAK_RAW)
    assert len(leaks) == 1 and leaks[0]["model"] == "m"


def test_detect_clean_proper_split():
    assert leakage.detect(CLEAN_SPLIT) == []


def test_detect_same_side_leak():
    assert len(leakage.detect(LEAK_SAME_SIDE)) == 1


def test_detect_different_sources_clean():
    assert leakage.detect(CLEAN_DIFF_SOURCES) == []


def _build(tmp_path, src):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text(src)
    conn = store.init_db(str(tmp_path / "g.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    leakage.analyze(conn, str(repo), fm)
    return conn


def test_gate_fails_on_leak(tmp_path):
    conn = _build(tmp_path, LEAK_RAW)
    r = selfcheck._check_no_data_leakage(conn)
    conn.close()
    assert r["ok"] is False and r["severity"] == "error"
    assert "leakage" in r["detail"].lower()


def test_gate_passes_when_clean(tmp_path):
    conn = _build(tmp_path, CLEAN_SPLIT)
    r = selfcheck._check_no_data_leakage(conn)
    conn.close()
    assert r["ok"] is True


# ── Phase 4.2 — unguarded model inputs ───────────────────────────────────────────
UNGUARDED = "def go(X, m):\n    m.predict(X)\n"
GUARDED_ASSERT = "def go(X, m):\n    assert X is not None\n    m.predict(X)\n"
GUARDED_VALIDATE = "def go(X, m):\n    schema.validate(X)\n    m.fit(X)\n"


def test_detect_unguarded_input():
    f = leakage.detect_unguarded_inputs(UNGUARDED)
    assert len(f) == 1 and f[0]["input"] == "X"


def test_guarded_by_assert_is_clean():
    assert leakage.detect_unguarded_inputs(GUARDED_ASSERT) == []


def test_guarded_by_validator_is_clean():
    assert leakage.detect_unguarded_inputs(GUARDED_VALIDATE) == []


def test_unguarded_gate_warns(tmp_path):
    conn = _build(tmp_path, UNGUARDED)
    r = selfcheck._check_unguarded_model_inputs(conn)
    conn.close()
    assert r["ok"] is False and r["severity"] == "warning"


# ── Phase 4.3 — edge resolution coverage ─────────────────────────────────────────
def test_resolution_coverage_surfaces_inferred(tmp_path):
    conn = store.init_db(str(tmp_path / "g.db"))
    nt = store.get_or_create_node_type(conn, "function")
    et = store.get_or_create_edge_type(conn, "calls")
    a = store.add_node(conn, nt, name="a", qualified_name="m.a")
    b = store.add_node(conn, nt, name="b", qualified_name="m.b")
    c = store.add_node(conn, nt, name="c", qualified_name="m.c")
    store.add_edge(conn, et, a, b, metadata={"confidence": "high"})
    store.add_edge(conn, et, a, c, metadata={"confidence": "inferred"})
    r = selfcheck._check_resolution_coverage(conn)
    conn.close()
    assert r["severity"] == "warning" and r["ok"] is False
    assert "1 ambiguous" in r["detail"]
