"""A refresh commits in batches, not once per row (FL-247).

`store.add_node`, `add_edge` and `_get_or_create` committed after every row. The
graph is a rollback-journal database, so each commit writes a journal and waits
on fsync twice; a refresh of this repository writes about 100,000 rows. On a
copy of its graph a full refresh took 1294 s, almost all of it in those waits,
and 27.5 s with one commit per analyzer phase — the same graph, row for row.

Inside `store.batched(conn)` the writers commit once COMMIT_EVERY row changes
have built up, so a reader is never held out longer than one batch takes to
write. Outside it they commit per row, as before. `cli.run` leaves the batch
before the history layer: its providers read the graph through a connection
of their own and must see every row the analyzers wrote (test_provider_lock).
"""
import sqlite3

import pytest

from analyzer import cards, cli, history, store, walker


@pytest.fixture
def conn(tmp_path):
    c = store.init_db(str(tmp_path / "demo-state-graph.db"))
    yield c
    c.close()


def _seen(path) -> int:
    """How many nodes another connection sees: what has been committed."""
    other = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    try:
        return other.execute("SELECT count(*) FROM node").fetchone()[0]
    finally:
        other.close()


def _path(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


def _add(conn, i: int) -> int:
    t = store.get_or_create_node_type(conn, "function")
    return store.add_node(conn, t, name=f"f{i}", qualified_name=f"pkg.m.f{i}", file_path="pkg/m.py")


# ── the writers ─────────────────────────────────────────────────────────────

def test_outside_a_batch_every_row_is_committed_at_once(conn):
    _add(conn, 0)
    assert _seen(_path(conn)) == 1 and not conn.in_transaction
    a = _add(conn, 1)
    store.add_edge(conn, store.get_or_create_edge_type(conn, "calls"), a, a)
    assert _seen(_path(conn)) == 2 and not conn.in_transaction


def test_inside_a_batch_rows_are_committed_every_commit_every_changes(conn, monkeypatch):
    monkeypatch.setattr(store, "COMMIT_EVERY", 3)
    store.get_or_create_node_type(conn, "function")   # committed before the batch opens
    seen = []
    with store.batched(conn):
        for i in range(7):
            _add(conn, i)
            seen.append(_seen(_path(conn)))
    assert seen == [0, 0, 3, 3, 3, 6, 6]
    assert _seen(_path(conn)) == 7 and not conn.in_transaction


def test_raw_writes_by_an_analyzer_count_towards_the_batch(conn, monkeypatch):
    monkeypatch.setattr(store, "COMMIT_EVERY", 3)
    with store.batched(conn):
        nid = _add(conn, 0)
        conn.execute("UPDATE node SET metadata_json = '{}' WHERE id = ?", (nid,))
        conn.execute("UPDATE node SET metadata_json = '{}' WHERE id = ?", (nid,))
        _add(conn, 1)          # 5 changes since the batch opened: node type, node, two updates, node
        assert _seen(_path(conn)) == 2


def test_a_batch_that_raises_still_commits_what_it_wrote(conn):
    """Per-row commits kept every row written before a failure; the batch keeps
    them too, so a failed refresh leaves the graph no worse than it did."""
    with pytest.raises(RuntimeError):
        with store.batched(conn):
            _add(conn, 0)
            _add(conn, 1)
            raise RuntimeError("an analyzer failed")
    assert _seen(_path(conn)) == 2 and not conn.in_transaction


def test_a_nested_batch_does_not_end_the_outer_one(conn, monkeypatch):
    monkeypatch.setattr(store, "COMMIT_EVERY", 100)
    with store.batched(conn):
        _add(conn, 0)
        with store.batched(conn):
            _add(conn, 1)
        assert conn.in_transaction and _seen(_path(conn)) == 0
    assert _seen(_path(conn)) == 2


# ── a whole refresh ─────────────────────────────────────────────────────────

def _repo(tmp_path):
    """Enough code for a few hundred rows: modules that call each other and
    pass frames along, plus SQL reads, API reads and typed columns."""
    repo = tmp_path / "repo"
    for m in range(12):
        body = [f"import pandas as pd\nfrom pkg import m{(m + 1) % 12}\n"]
        for f in range(8):
            body.append(f"def f{f}(df):\n    out = df[df.q > {f}]\n    out = out.assign(r=out.q * 2)\n"
                        f"    return m{(m + 1) % 12}.f{(f + 1) % 8}(out)\n")
        p = repo / "pkg" / f"m{m}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(body))
    # the analyzers that write outside add_node/add_edge: sql_refs and api_refs
    # merge an assumed schema into a node, data_model sets a column's dtype
    io = ["import pandas as pd\nimport requests\n"]
    for k in range(10):
        io.append(f'def load{k}():\n    return run("SELECT store_id, amount, ts FROM `demo.retail.sales{k}`")\n')
        io.append(f'def fetch{k}():\n    resp = requests.get("https://api.example.com/u{k}")\n'
                  f'    data = resp.json()\n    return data["name"], data["email"]\n')
        io.append(f"def typed{k}():\n    df = pd.DataFrame({{'a{k}': [1]}})\n"
                  f"    df['a{k}'] = df['a{k}'].astype('int64')\n    return df\n")
    (repo / "pkg" / "io.py").write_text("\n".join(io))
    (repo / "pkg" / "__init__.py").write_text("")
    return repo


class _Trace:
    """Every statement cli.run's connection runs, with total_changes beside it,
    plus where the analyzer phases start (walk) and end (cards), and whether
    the host is mid-transaction when the history layer starts."""

    def __init__(self, monkeypatch):
        self.log, self.marks, self.at_snapshot = [], {}, None
        real_init, real_walk, real_snap = store.init_db, walker.walk, history.snapshot_run
        real_cards = cards.build_symbol_cards

        def init_db(path):
            c = real_init(path)
            c.set_trace_callback(lambda s: self.log.append((s.strip().upper(), c.total_changes)))
            return c

        def walk(c, repo):
            self.marks["walk"] = len(self.log)
            return real_walk(c, repo)

        def build_symbol_cards(c):
            self.marks["cards"] = len(self.log)
            return real_cards(c)

        def snapshot_run(c, *a, **k):
            self.marks["snapshot"] = len(self.log)
            self.at_snapshot = c.in_transaction
            return real_snap(c, *a, **k)

        monkeypatch.setattr(store, "init_db", init_db)
        monkeypatch.setattr(walker, "walk", walk)
        monkeypatch.setattr(history, "snapshot_run", snapshot_run)
        monkeypatch.setattr(cards, "build_symbol_cards", build_symbol_cards)

    def commits(self, lo=0, hi=None):
        return [n for s, n in self.log[lo:hi] if s == "COMMIT"]


def _rows(path) -> int:
    c = sqlite3.connect(path)
    try:
        return sum(c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("node", "edge"))
    finally:
        c.close()


def test_a_refresh_commits_far_fewer_times_than_it_writes_rows(tmp_path, monkeypatch):
    tr = _Trace(monkeypatch)
    db = str(tmp_path / "demo-state-graph.db")
    cli.run(str(_repo(tmp_path)), "demo", db)
    rows = _rows(db)
    assert rows > 300, rows
    analyzers = tr.commits(tr.marks["walk"], tr.marks["cards"])
    assert len(analyzers) <= 3, f"{len(analyzers)} commits while the analyzers wrote {rows} rows"
    assert len(tr.commits()) * 10 < rows


def test_no_batch_holds_more_than_about_commit_every_changes(tmp_path, monkeypatch):
    """What bounds how long a reader can be held out: no transaction in the
    analyzer phases changes much more than COMMIT_EVERY rows."""
    monkeypatch.setattr(store, "COMMIT_EVERY", 40)
    tr = _Trace(monkeypatch)
    cli.run(str(_repo(tmp_path)), "demo", str(tmp_path / "demo-state-graph.db"))
    lo, hi = tr.marks["walk"], tr.marks["cards"]
    start = max((n for s, n in tr.log[:lo] if s == "COMMIT"), default=0)
    sizes, last = [], start
    for n in tr.commits(lo, hi):
        sizes.append(n - last)
        last = n
    assert len(sizes) > 5 and max(sizes) <= 2 * 40, sizes


def test_the_history_layer_starts_with_everything_committed(tmp_path, monkeypatch):
    """The providers read through ctx.conn_ro; rows still in a transaction on
    the host connection would be invisible or locked to them, and a degraded
    provider leaves nodes without a node_key."""
    tr = _Trace(monkeypatch)
    report = {}
    real = history.snapshot_run
    monkeypatch.setattr(history, "snapshot_run",
                        lambda c, *a, **k: real(c, *a, **dict(k, report=report)))
    db = str(tmp_path / "demo-state-graph.db")
    cli.run(str(_repo(tmp_path)), "demo", db)
    assert tr.at_snapshot is False
    assert report and not [p for p, r in report.items() if r["degraded"]], report
    c = sqlite3.connect(db)
    try:
        assert c.execute("SELECT count(*) FROM node_snapshot WHERE node_key IS NULL").fetchone()[0] == 0
    finally:
        c.close()


_SNAPSHOT = "SELECT qualified_name, node_type, node_key, struct_sig FROM node_snapshot ORDER BY 1, 2"
_EVENTS = "SELECT seq, event_type, node_key, tier, payload_json FROM node_event ORDER BY 1"
_NODES = ("SELECT n.qualified_name, t.name, n.file_path, n.line_start, n.line_end, n.metadata_json, n.node_key "
          "FROM node n JOIN node_type t ON t.id = n.node_type_id ORDER BY 1, 2, 3, 4")
_EDGES = ("SELECT s.qualified_name, d.qualified_name, t.name, e.metadata_json FROM edge e "
          "JOIN node s ON s.id = e.src_node_id JOIN node d ON d.id = e.dst_node_id "
          "JOIN edge_type t ON t.id = e.edge_type_id ORDER BY 1, 2, 3, 4")


def test_a_batched_refresh_writes_the_graph_a_per_row_one_does(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    graphs = {}
    for every in (1, store.COMMIT_EVERY):
        monkeypatch.setattr(store, "COMMIT_EVERY", every)
        (tmp_path / f"g{every}").mkdir()
        db = str(tmp_path / f"g{every}" / "demo-state-graph.db")
        cli.run(str(repo), "demo", db)
        c = sqlite3.connect(db)
        graphs[every] = {q: c.execute(q).fetchall() for q in (_NODES, _EDGES, _SNAPSHOT, _EVENTS)}
        c.close()
    one, batched = graphs.values()
    assert one[_NODES] and one[_EDGES] and one[_SNAPSHOT]
    for q in one:
        assert one[q] == batched[q], q[:40]
