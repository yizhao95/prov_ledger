"""`provledger.declared` against the six contracts (docs/conformance.md).

The package-native context is an EMPTY graph, and a declared node lives in the
graph, so the suite would have nothing to look at and check 3 would pass
vacuously. Hosts with a real graph inject `context_factory`, which is exactly
what this test does: it builds the graph the analyzer would have built for each
corpus repository — one node per function, plus one declared business rule that
links to `main` and `clean`.

That makes the mutations real evidence: `rename_function` renames `main`,
`delete_function` deletes `clean` and `move_file` moves it to another module,
so the rule's dataflow signature moves with them while its identity — its slug —
holds. "preserved for every mutation" is therefore a claim the corpus tests,
not a claim the corpus ignores.
"""
import ast
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from orchestrator import graph_api as g
from orchestrator.testing import conformance

SIX = ["determinism", "purity", "stability_matches_declaration", "schema", "failure_isolation", "performance_budget"]
LINKED = ("main", "clean")


@pytest.fixture
def provider():
    from orchestrator.providers.declared_provider import DeclaredProvider
    return DeclaredProvider()


def _functions(repo: Path) -> list[str]:
    """Qualified names the analyzer would mint for this repository."""
    out = []
    for py in sorted(repo.rglob("*.py")):
        module = str(py.relative_to(repo))[:-3].replace(os.sep, ".").replace("/", ".")
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(f"{module}.{node.name}")
    return sorted(set(out))


def _context(repo: Path, *, run_id: int = 1) -> g.ExtractionContext:
    repo = Path(repo)
    names = _functions(repo)
    db = os.path.join(tempfile.mkdtemp(prefix="provledger-declared-conformance-"), "graph.db")
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT, qualified_name TEXT,
                           file_path TEXT, line_start INTEGER, line_end INTEGER, metadata_json TEXT,
                           dtype TEXT, run_id INTEGER);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT, confidence TEXT, run_id INTEGER);
        INSERT INTO node_type (id, name) VALUES (1, 'function'), (2, 'business_rule');
    """)
    for i, qn in enumerate(names, start=1):
        c.execute("INSERT INTO node (id, node_type_id, name, qualified_name) VALUES (?, 1, ?, ?)",
                  (i, qn.rsplit(".", 1)[-1], qn))
    # the declaration is fixed: it names the same two functions whatever the repo says
    links = [{"to": qn, "kind": "declared_constrains", "by": "user"}
             for qn in sorted(n for n in names if n.rsplit(".", 1)[-1] in LINKED)]
    meta = {"declared_id": 1, "slug": "emea-excluded", "node_type": "business_rule", "state": "active",
            "tier": "stated", "version": 2, "description": "EMEA excluded from the Q3 rollup",
            "attrs": {"scope": "Q3"}, "links": links, "links_checked": 1,
            "field_tiers": {"node_type": "stated", "links": ["stated"] * len(links)}}
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, metadata_json) VALUES (900, 2, ?, ?, ?)",
              ("emea-excluded", "declared:emea-excluded", json.dumps(meta, sort_keys=True)))
    c.commit()
    c.close()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)

    def node_rows(kinds):
        ph = ",".join("?" for _ in kinds) or "''"
        return conn.execute(f"SELECT n.id, t.name, n.qualified_name, n.name, n.file_path, n.line_start, n.line_end, n.dtype "
                            f"FROM node n JOIN node_type t ON t.id = n.node_type_id WHERE t.name IN ({ph}) ORDER BY n.id",
                            tuple(kinds)).fetchall()

    return g.ExtractionContext(repo_root=str(repo), conn_ro=conn, run_id=run_id, file_map={}, node_rows=node_rows)


def test_declared_passes_all_six_contracts(provider):
    rep = conformance.run(provider, context_factory=_context)
    assert [c["name"] for c in rep.checks] == SIX
    assert rep.ok is True, [c for c in rep.checks if not c["ok"]]
    assert all(c["ok"] for c in rep.checks), [c for c in rep.checks if not c["ok"]]


def test_the_declaration_is_actually_exercised_by_the_corpus(provider):
    """A vacuous pass is the failure mode this check exists to avoid: the report
    must name the mutations that reached the declared node."""
    rep = conformance.run(provider, context_factory=_context)
    stability = next(c for c in rep.checks if c["name"] == "stability_matches_declaration")
    assert "no observation of this provider" not in stability["detail"]
    for mutation in ("rename_function", "delete_function", "move_file"):
        assert mutation in stability["detail"]


def test_a_declared_node_survives_every_mutation_it_declares_to_survive(provider):
    declared_stability = provider.declared_stability()
    assert set(declared_stability) == set(g.MUTATIONS)
    assert set(declared_stability.values()) == {"preserved"}
