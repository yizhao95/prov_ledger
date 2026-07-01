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
