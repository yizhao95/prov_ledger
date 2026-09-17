"""provledger.declared — the provider that projects declared nodes into the graph.

A declared node's identity is its slug, its STRUCT signature is the declaration's
attributes and its DATAFLOW signature is the links it draws to code — including
whether each link still lands on a node that exists. So retiring a declaration,
editing its attributes, or deleting the function it points at are all ordinary
computed changes, exactly like a function's.

The provider reads only the graph it was handed (`ctx.conn_ro`): it never opens
the orchestrator database, so it stays pure under the conformance suite.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from orchestrator import graph_api as g
from orchestrator import providers

DECL = "provledger.declared"


def _graph(tmp_path, declared, code=("pkg.rollup.quarterly_revenue", "pkg.rollup.emea_share")):
    """A graph-shaped DB: `code` function nodes plus one node per `declared`
    entry (the rows the analyzer's declared stage writes)."""
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "graph.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT, qualified_name TEXT,
                           file_path TEXT, line_start INTEGER, line_end INTEGER, metadata_json TEXT,
                           dtype TEXT, run_id INTEGER);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT, confidence TEXT, run_id INTEGER);
    """)
    c.execute("INSERT INTO node_type (id, name) VALUES (1, 'function')")
    for i, qn in enumerate(code, start=1):
        c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path) VALUES (?, 1, ?, ?, 'pkg/rollup.py')",
                  (i, qn.rsplit(".", 1)[-1], qn))
    tid = 2
    for j, d in enumerate(declared, start=100):
        c.execute("INSERT OR IGNORE INTO node_type (id, name) VALUES (?, ?)", (tid, d["node_type"]))
        row = c.execute("SELECT id FROM node_type WHERE name = ?", (d["node_type"],)).fetchone()[0]
        tid += 1
        c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, metadata_json) VALUES (?, ?, ?, ?, ?)",
                  (j, row, d["slug"], "declared:" + d["slug"], json.dumps(d)))
    c.commit()
    c.close()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)

    def node_rows(kinds):
        ph = ",".join("?" for _ in kinds) or "''"
        return conn.execute(f"SELECT n.id, t.name, n.qualified_name, n.name, n.file_path, n.line_start, n.line_end, n.dtype "
                            f"FROM node n JOIN node_type t ON t.id = n.node_type_id WHERE t.name IN ({ph}) ORDER BY n.id",
                            tuple(kinds)).fetchall()

    return g.ExtractionContext(repo_root=str(tmp_path), conn_ro=conn, run_id=1, file_map={}, node_rows=node_rows)


def _decl(slug="emea-excluded", node_type="business_rule", attrs=None, links=(), state="active", tier="stated", version=2):
    return {"declared_id": 1, "slug": slug, "node_type": node_type, "state": state, "tier": tier, "version": version,
            "description": "EMEA excluded from the Q3 rollup", "attrs": dict(attrs or {}),
            "links": [{"to": t, "kind": k, "by": "user"} for t, k in links], "links_checked": 1,
            "field_tiers": {"node_type": "stated", "links": ["stated"] * len(links)}}


@pytest.fixture
def provider():
    from orchestrator.providers.declared_provider import DeclaredProvider
    return DeclaredProvider()


def test_declared_is_a_builtin_provider_alongside_symbol_and_owned():
    ids = [p.type_id for p in providers.builtin_providers()]
    assert ids == ["provledger.symbol", "provledger.owned", DECL]
    assert all(isinstance(p, g.NodeTypeProvider) for p in providers.builtin_providers())


def test_one_active_declaration_becomes_one_observation(tmp_path, provider):
    ctx = _graph(tmp_path, [_decl(attrs={"scope": "Q3"})])
    obs = provider.extract(ctx)
    assert len(obs) == 1
    o = obs[0]
    assert (o.type_id, o.node_type, o.qualified_name) == (DECL, "business_rule", "declared:emea-excluded")
    assert o.file_path is None and o.line_start is None
    assert o.attrs["declared_tier"] == "stated" and o.attrs["declared_attrs"] == {"scope": "Q3"}
    assert "tier" not in o.attrs, "the host decides tiers; a provider never claims one"


def test_the_struct_signature_is_the_attributes_and_moves_when_they_do(tmp_path, provider):
    a = provider.extract(_graph(tmp_path / "a", [_decl(attrs={"scope": "Q3"})]))[0]
    b = provider.extract(_graph(tmp_path / "b", [_decl(attrs={"scope": "Q3"})]))[0]
    c = provider.extract(_graph(tmp_path / "c", [_decl(attrs={"scope": "Q3 and Q4"})]))[0]
    assert a.signature("struct").value == b.signature("struct").value
    assert a.signature("struct").value != c.signature("struct").value


def test_the_dataflow_signature_is_the_links_and_is_trivial_without_them(tmp_path, provider):
    none = provider.extract(_graph(tmp_path / "n", [_decl()]))[0]
    assert none.signature("dataflow").trivial is True
    one = provider.extract(_graph(tmp_path / "o", [_decl(links=[("pkg.rollup.quarterly_revenue", "declared_constrains")])]))[0]
    assert one.signature("dataflow").trivial is False and one.signature("dataflow").value
    assert one.attrs["links_total"] == 1 and one.attrs["links_resolved"] == 1


def test_the_dataflow_signature_changes_when_the_link_target_leaves_the_graph(tmp_path, provider):
    links = [("pkg.rollup.quarterly_revenue", "declared_constrains")]
    here = provider.extract(_graph(tmp_path / "1", [_decl(links=links)]))[0]
    gone = provider.extract(_graph(tmp_path / "2", [_decl(links=links)], code=("pkg.rollup.emea_share",)))[0]
    assert here.signature("dataflow").value != gone.signature("dataflow").value
    assert gone.attrs["links_resolved"] == 0 and gone.attrs["links_total"] == 1


def test_a_draft_or_retired_declaration_is_not_in_the_graph(tmp_path, provider):
    ctx = _graph(tmp_path, [_decl(slug="a", state="draft"), _decl(slug="b", state="retired"),
                            _decl(slug="c", state="active")])
    assert [o.qualified_name for o in provider.extract(ctx)] == ["declared:c"]


def test_the_observation_carries_the_declaration_tier_never_the_host_tier(tmp_path, provider):
    ctx = _graph(tmp_path, [_decl(slug="a", tier="stated"), _decl(slug="b", tier="asserted")])
    tiers = {o.qualified_name: o.attrs["declared_tier"] for o in provider.extract(ctx)}
    assert tiers == {"declared:a": "stated", "declared:b": "asserted"}
    assert all(t in ("stated", "asserted") for t in tiers.values()), "a declared node is never observed"


def test_run_provider_accepts_the_declared_provider_without_degrading(tmp_path, provider):
    ctx = _graph(tmp_path, [_decl(attrs={"scope": "Q3"}, links=[("pkg.rollup.emea_share", "declared_feeds")])])
    obs, degraded, elapsed = providers.run_provider(provider, ctx, timeout_s=10)
    assert degraded is None and len(obs) == 1 and elapsed >= 0


def test_extraction_is_deterministic(tmp_path, provider):
    ctx = _graph(tmp_path, [_decl(slug="b"), _decl(slug="a", attrs={"x": 1})])
    first = [g.observation_json(o) for o in sorted(provider.extract(ctx), key=g.observation_key)]
    second = [g.observation_json(o) for o in sorted(provider.extract(ctx), key=g.observation_key)]
    assert first == second and len(first) == 2
