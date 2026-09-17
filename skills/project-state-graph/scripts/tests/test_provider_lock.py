"""A provider must not be locked out by the provider that ran before it
(found while closing DP phase 3; the analyzer half of it).

`history.snapshot_run` runs providers in a loop and `emit()` writes
`node_snapshot` rows on the host connection, while every provider reads the
same file through `ctx.conn_ro`. Left uncommitted, that write transaction grows
with the graph: on a 222 MB graph it outlived the next provider's busy timeout
and `provledger.owned` — the provider that gives dataframe/column nodes their
identity — degraded with `OperationalError: database is locked`. 129 nodes then
carried no node_key and selfcheck's `history_key_coverage` failed, so a refresh
that had otherwise succeeded exited 1.

The symptom is size-dependent and therefore not reproducible in a unit test.
The invariant behind it is not: when a provider is asked to extract, the host
must not be sitting in an uncommitted write transaction.
"""
from pathlib import Path

from analyzer import _host, history, store
from tests.test_history import BASE, conn  # noqa: F401

providers = _host.providers
graph_api = _host.graph_api


class _Emitter:
    """Writes enough snapshot rows that the host is certainly mid-transaction."""
    type_id = "acme.emitter"
    schema_version = 1
    requires: tuple = ()

    def extract(self, ctx):
        return [graph_api.NodeObservation(
            type_id=self.type_id, node_type="dataframe", qualified_name=f"pkg.m.f:df{i}",
            file_path="pkg/m.py", line_start=i, line_end=i,
            signatures=(graph_api.Signature("qualname", f"pkg.m.f:df{i}"),),
            attrs={}, owner_qn="pkg.m.f", name=f"df{i}") for i in range(200)]

    def attributes_schema(self):
        return {}

    def declared_stability(self):
        return {m: "preserved" for m in graph_api.MUTATIONS}


def _reader(record, host_conn):
    class _Reader:
        type_id = "acme.reader"
        schema_version = 1
        requires: tuple = ()

        def extract(self, ctx):
            record.append({"host_in_transaction": host_conn.in_transaction,
                           "read_ok": ctx.node_rows(("function",)) is not None})
            return []

        def attributes_schema(self):
            return {}

        def declared_stability(self):
            return {m: "preserved" for m in graph_api.MUTATIONS}
    return _Reader()


def _prepare(conn, tmp_path):
    for rel, src in BASE.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    from analyzer import py_ast, walker
    run_id = store.start_run(conn, project_name="demo", commit_sha="c")
    fm = walker.walk(conn, str(tmp_path))
    py_ast.analyze(conn, str(tmp_path), fm)
    return run_id


def test_a_provider_never_reads_while_the_one_before_it_is_still_uncommitted(conn, tmp_path):
    run_id = _prepare(conn, tmp_path)
    record: list = []
    history.snapshot_run(conn, str(tmp_path), run_id, providers=[_Emitter(), _reader(record, conn)])
    assert len(record) == 1
    assert record[0]["host_in_transaction"] is False, (
        "the host was still inside an uncommitted write transaction when the next provider ran: "
        "on a large graph that is how provledger.owned gets locked out and 129 nodes lose their node_key")
    assert record[0]["read_ok"] is True


def test_the_read_only_connection_waits_instead_of_failing_immediately(tmp_path):
    """Defence in depth: even with a commit between providers, a provider's own
    read must be willing to wait for a writer rather than degrading at once."""
    db = tmp_path / "g.db"
    c = store.init_db(str(db))
    c.commit()
    c.close()
    ctx = providers.make_context(str(db), str(tmp_path), 1, {})
    try:
        timeout_ms = ctx.conn_ro.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        ctx.conn_ro.close()
    assert timeout_ms >= 30000, "a provider's read-only connection must wait out a host write, not degrade on it"
