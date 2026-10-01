"""`provledger graph` — the project graph and its relations, folded (spec §10.2/§10.3).

§10.3 is arithmetic, not taste: the live project's node names alone cost ~147k
tokens and its edges number 68384, so no read can hand the graph over flat. The
answer is to FOLD, and the section is explicit that folding is not selecting —
we never rank, score or pre-pick what is relevant, we only decide how much to
print at once, and we always say what was folded and the exact command that
unfolds it. Every test below is about one of those two halves.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

from orchestrator import graph_view, provenance as pv

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402


@pytest.fixture
def graph(tmp_path):
    """Two areas (app/core, app/web), three imported modules with no file of
    their own, and edges of three kinds — the live graph's shape in miniature."""
    path = tmp_path / "g.db"
    c = ps.build(path)
    ps.add_run(c, 1)
    c.executescript("""
        INSERT INTO node_type (id, name) VALUES (1, 'file'), (2, 'function'), (3, 'module'), (4, 'data_var');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES
            (1, 1, 'load.py',  'app/core/load.py',    'app/core/load.py', 1, NULL),
            (2, 2, 'read',     'app.core.load.read',  'app/core/load.py', 1, 'nk_read'),
            (3, 2, 'parse',    'app.core.load.parse', 'app/core/load.py', 1, 'nk_parse'),
            (4, 4, 'src',      'read:param:src',      'app/core/load.py', 1, NULL),
            (5, 1, 'util.py',  'app/core/util.py',    'app/core/util.py', 1, NULL),
            (6, 2, 'helper',   'app.core.util.helper','app/core/util.py', 1, 'nk_helper'),
            (7, 1, 'view.py',  'app/web/view.py',     'app/web/view.py',  1, NULL),
            (8, 2, 'render',   'app.web.view.render', 'app/web/view.py',  1, 'nk_render'),
            (9, 3, 'json',     'json',                NULL,               1, NULL),
            (10, 3, 'json',    'json',                NULL,               1, NULL),
            (11, 3, 'pathlib', 'pathlib',             NULL,               1, NULL);
        CREATE TABLE IF NOT EXISTS edge_type (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER NOT NULL, src_node_id INTEGER NOT NULL,
                                         dst_node_id INTEGER NOT NULL, metadata_json TEXT, run_id INTEGER, confidence TEXT);
        INSERT INTO edge_type (id, name) VALUES (1, 'calls'), (2, 'imports'), (3, 'feeds');
        INSERT INTO edge (edge_type_id, src_node_id, dst_node_id) VALUES
            (1, 2, 3), (1, 8, 2), (3, 3, 4), (2, 1, 9), (2, 5, 10), (2, 7, 11);
    """)
    c.commit(); c.close()
    return str(path)


@pytest.fixture
def seeded(conn):
    """Two records on nk_read; app/web carries none — the one thing only the
    root view can tell a reader cheaply."""
    a = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_read", kind="technical",
                         interpretation="read() streams instead of slurping", recorded_by="agent")
    b = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_read", kind="technical",
                         role="rejected_path", interpretation="tried mmap", rule_id="R6", recorded_by="system")
    conn.commit()
    return {"a": a, "b": b}


def test_the_root_view_folds_to_areas_with_node_counts_and_how_many_carry_records(conn, graph, seeded):
    doc = graph_view.project_graph(graph, ledger_conn=conn, project="proj")
    by = {a["area"]: a for a in doc["areas"]}
    assert by["app/core"]["nodes"] == 6 and by["app/core"]["with_records"] == 1
    assert by["app/web"]["nodes"] == 2 and by["app/web"]["with_records"] == 0
    assert doc["totals"] == {"nodes": 11, "nodes_shown": 8, "nodes_excluded": 3, "with_records": 1,
                             "edges": 6, "edges_shown": 3, "edges_excluded": 3}


def test_the_root_view_lists_the_relation_vocabulary_of_the_graph_it_shows(conn, graph, seeded):
    doc = graph_view.project_graph(graph, ledger_conn=conn, project="proj")
    assert doc["relations"] == [{"edge_type": "calls", "edges": 2}, {"edge_type": "feeds", "edges": 1}]
    assert "imports" not in graph_view.render(doc).split("excluded")[0]


def test_imported_modules_are_excluded_by_a_rule_the_graph_itself_encodes(conn, graph, seeded):
    """An `imports`-edge destination with no file in this repo is a module the
    project imports, not code the project owns. That is the rule — not a
    blocklist of names — and it is overridable and never silent."""
    doc = graph_view.project_graph(graph, ledger_conn=conn, project="proj")
    assert "(external)" not in [a["area"] for a in doc["areas"]]
    assert doc["excluded"]["nodes"] == 3 and doc["excluded"]["names"][0] == {"name": "json", "nodes": 2}
    assert "imports" in doc["excluded"]["rule"] and doc["excluded"]["include_with"] == "--include-imports"
    assert str(doc["excluded"]["nodes"]) in graph_view.render(doc)
    kept = graph_view.project_graph(graph, ledger_conn=conn, project="proj", include_imports=True)
    assert {"area": "(external)", "nodes": 3, "with_records": 0} in [
        {k: a[k] for k in ("area", "nodes", "with_records")} for a in kept["areas"]]
    assert kept["totals"]["nodes_excluded"] == 0 and {"edge_type": "imports", "edges": 3} in kept["relations"]


def test_every_area_row_carries_the_exact_command_that_unfolds_it(conn, graph, seeded):
    doc = graph_view.project_graph(graph, ledger_conn=conn, project="proj")
    assert [a["unfold"] for a in doc["areas"]] == ["provledger graph app/core --depth 1",
                                                   "provledger graph app/web --depth 1"]
    assert "provledger graph app/core --depth 1" in graph_view.render(doc)


def test_unfolding_an_area_prints_node_names_not_counts(conn, graph, seeded):
    """The single most important requirement: a name a model can feed straight
    back into another read."""
    doc = graph_view.unfold(graph, "app/core", depth=1, ledger_conn=conn, project="proj")
    assert doc["view"] == "path"
    files = {g["path"]: g for g in doc["groups"]}
    assert files["app/core/load.py"]["nodes"] == 4 and files["app/core/util.py"]["nodes"] == 2
    assert [n["qualified_name"] for n in files["app/core/load.py"]["names"]] == [
        "app.core.load.parse", "app.core.load.read", "app/core/load.py", "read:param:src"]
    read = next(n for n in files["app/core/load.py"]["names"] if n["qualified_name"] == "app.core.load.read")
    assert read["node_type"] == "function" and read["records"] == 2 and read["node_key"] == "nk_read"
    text = graph_view.render(doc)
    assert "app.core.load.read" in text and "function" in text


def test_a_directory_deeper_than_the_requested_depth_is_a_count_plus_its_command(conn, graph, seeded):
    doc = graph_view.unfold(graph, "app", depth=1, ledger_conn=conn, project="proj")
    assert doc["resolved_as"] == "path prefix"          # `app` is a prefix; the areas are app/core and app/web
    groups = {g["path"]: g for g in doc["groups"]}
    assert groups["app/core"]["nodes"] == 6 and groups["app/core"].get("names") is None
    assert groups["app/core"]["unfold"] == "provledger graph app/core --depth 1"
    deeper = graph_view.unfold(graph, "app", depth=2, ledger_conn=conn, project="proj")
    assert any(g["path"] == "app/core/load.py" and g.get("names") for g in deeper["groups"])


def test_unfolding_a_node_prints_its_neighbours_by_name_with_the_edge_type_on_each(conn, graph, seeded):
    doc = graph_view.unfold(graph, "app.core.load.read", depth=1, ledger_conn=conn, project="proj")
    assert doc["view"] == "node" and doc["node"]["qualified_name"] == "app.core.load.read"
    hop1 = {(n["qualified_name"], n["edge_type"], n["direction"]) for n in doc["hops"][0]["neighbours"]}
    assert hop1 == {("app.core.load.parse", "calls", "out"), ("app.web.view.render", "calls", "in")}
    assert doc["hops"][0]["hop"] == 1
    deep = graph_view.unfold(graph, "app.core.load.read", depth=2, ledger_conn=conn, project="proj")
    assert [n["qualified_name"] for n in deep["hops"][1]["neighbours"]] == ["read:param:src"]
    assert deep["hops"][1]["neighbours"][0]["edge_type"] == "feeds"
    text = graph_view.render(deep)
    assert "calls" in text and "app.core.load.parse" in text and "read:param:src" in text


def test_a_node_unfold_carries_the_command_for_each_neighbour_and_the_record_count(conn, graph, seeded):
    doc = graph_view.unfold(graph, "app.web.view.render", depth=1, ledger_conn=conn, project="proj")
    nb = doc["hops"][0]["neighbours"][0]
    assert nb["qualified_name"] == "app.core.load.read" and nb["records"] == 2
    assert nb["unfold"] == "provledger graph app.core.load.read --depth 1"
    text = graph_view.render(doc)
    assert nb["unfold"] in text and "records 2" in text                   # the name, its size, and what opens it
    assert doc["history"] == "provledger why app.web.view.render" and "provledger why <node>" in text


def test_a_list_over_the_limit_says_how_many_are_folded_and_the_command_that_shows_them(conn, graph, seeded):
    doc = graph_view.unfold(graph, "app/core/load.py", depth=1, limit=2, ledger_conn=conn, project="proj")
    g = doc["groups"][0]
    assert len(g["names"]) == 2 and g["folded"] == 2
    assert g["unfold"] == "provledger graph app/core/load.py --depth 1 --limit 4"
    assert "2 of 4" in graph_view.render(doc)
    nodes = graph_view.unfold(graph, "app.core.load.read", depth=1, limit=1, ledger_conn=conn, project="proj")
    assert nodes["hops"][0]["folded"] == 1
    assert nodes["hops"][0]["unfold"] == "provledger graph app.core.load.read --depth 1 --limit 2"


def test_a_slash_means_a_place_in_the_repo_even_when_a_node_carries_that_name(conn, graph, seeded):
    """`app/core/load.py` is both a path and a `file` node's qualified name. The
    area row's command has to open the file's CONTENTS, or the fold chain the
    root view promises (area → file → names) dead-ends at the file node."""
    doc = graph_view.unfold(graph, "app/core/load.py", depth=1, ledger_conn=conn, project="proj")
    assert doc["view"] == "path" and doc["groups"][0]["nodes"] == 4
    assert graph_view.unfold(graph, "app.core.load.read", depth=1, ledger_conn=conn, project="proj")["view"] == "node"


def test_an_unknown_target_says_so_and_suggests_the_root_view(conn, graph, seeded):
    doc = graph_view.unfold(graph, "nothing.like.this", depth=1, ledger_conn=conn, project="proj")
    assert doc["view"] == "unknown" and "provledger graph" in graph_view.render(doc)


def test_a_missing_graph_degrades_visibly_instead_of_raising(conn, tmp_path):
    doc = graph_view.project_graph(str(tmp_path / "nope.db"), ledger_conn=conn, project="proj")
    assert doc["areas"] == [] and doc["graph"] is None and "no state graph" in graph_view.render(doc)


def test_the_graph_read_writes_nothing_at_all(conn, graph, seeded):
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("change_reason", "read_hit", "influence", "utterance")]
    graph_view.project_graph(graph, ledger_conn=conn, project="proj")
    graph_view.unfold(graph, "app/core", depth=2, ledger_conn=conn, project="proj")
    graph_view.unfold(graph, "app.core.load.read", depth=2, ledger_conn=conn, project="proj")
    after = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("change_reason", "read_hit", "influence", "utterance")]
    assert before == after and after[1] == 0
    g = sqlite3.connect(graph)
    assert g.execute("SELECT COUNT(*) FROM node").fetchone()[0] == 11          # the graph is untouched too
    g.close()


# ── caught on the live graph: an area row's command has to mean its own count ─

@pytest.fixture
def awkward(tmp_path):
    """The two shapes the live graph has and the miniature above did not: a node
    with no file at all (`subsystem_of` calls that area `(external)`), and an
    area key that is ALSO a prefix of two other areas."""
    path = tmp_path / "g2.db"
    c = ps.build(path)
    ps.add_run(c, 1)
    c.executescript("""
        INSERT INTO node_type (id, name) VALUES (1, 'file'), (2, 'function'), (3, 'sql_table');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES
            (1, 3, 'orders',  'orders',                  NULL,                  1, 'nk_orders'),
            (2, 3, 'clients', 'clients',                 NULL,                  1, NULL),
            (3, 2, 'top',     'back.top',                'back/top.py',         1, 'nk_top'),
            (4, 2, 'deep',    'back.tests.deep',         'back/tests/deep.py',  1, 'nk_deep'),
            (5, 2, 'deeper',  'back.tests.more.deeper',  'back/tests/more.py',  1, NULL),
            (6, 2, 'single',  'loose',                   'loose.py',            1, NULL);
    """)
    c.commit(); c.close()
    return str(path)


def test_an_area_of_nodes_with_no_file_can_be_unfolded_by_the_command_it_prints(conn, awkward):
    """`(external)` and `(root)` are names `subsystem_of` invents, not paths, so
    a prefix match can never find them — the root view was printing a command
    that resolved to nothing."""
    root = graph_view.project_graph(awkward, ledger_conn=conn, project="proj")
    ext = next(a for a in root["areas"] if a["area"] == "(external)")
    assert ext["nodes"] == 2 and ext["unfold"] == "provledger graph (external) --depth 1"
    doc = graph_view.unfold(awkward, "(external)", depth=1, ledger_conn=conn, project="proj")
    assert doc["view"] == "path" and doc["nodes"] == 2
    assert [n["qualified_name"] for n in doc["groups"][0]["names"]] == ["clients", "orders"]
    rootrow = next(a for a in root["areas"] if a["area"] == "(root)")
    assert graph_view.unfold(awkward, "(root)", depth=1, ledger_conn=conn, project="proj")["nodes"] == rootrow["nodes"]


def test_an_area_key_that_is_also_a_prefix_unfolds_to_its_own_count(conn, awkward):
    """`back` is an area of 1 node AND a prefix of 3. The row said 1 and its
    command showed 3, which is the same contradiction as `callers 12` beside
    `callers 38` — a folded count that its own command does not reproduce."""
    root = graph_view.project_graph(awkward, ledger_conn=conn, project="proj")
    back = next(a for a in root["areas"] if a["area"] == "back")
    assert back["nodes"] == 1
    doc = graph_view.unfold(awkward, "back", depth=1, ledger_conn=conn, project="proj")
    assert doc["nodes"] == back["nodes"] and doc["resolved_as"] == "area"
    assert "resolved as an area" in graph_view.render(doc)
    # `back/tests` is itself an area key, so it too answers with its own count
    subtree = graph_view.unfold(awkward, "back/tests", depth=1, ledger_conn=conn, project="proj")
    assert subtree["nodes"] == 2 and subtree["resolved_as"] == "area"
    assert {a["area"]: a["nodes"] for a in root["areas"]}["back/tests"] == 2


# ── also caught on the live graph ───────────────────────────────────────────

def test_one_neighbour_name_is_one_line_however_many_graph_nodes_carry_it(conn, graph, seeded):
    """`review_and_complete` printed `consumes ← _close:param:conn` twice, because
    two node rows carry that qualified name. Two identical lines read as a bug
    and waste the budget; the count belongs on one line."""
    g = sqlite3.connect(graph)
    g.executescript("""
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES
            (20, 2, 'parse', 'app.core.load.parse', 'app/core/other.py', 1, NULL);
        INSERT INTO edge (edge_type_id, src_node_id, dst_node_id) VALUES (1, 2, 20);
    """)
    g.commit(); g.close()
    doc = graph_view.unfold(graph, "app.core.load.read", depth=1, ledger_conn=conn, project="proj")
    parse = [n for n in doc["hops"][0]["neighbours"] if n["qualified_name"] == "app.core.load.parse"]
    assert len(parse) == 1 and parse[0]["nodes"] == 2
    assert graph_view.render(doc).count("app.core.load.parse ·") == 1


def test_a_group_says_what_kinds_of_node_it_holds_and_lets_the_reader_pick_one(conn, graph, seeded):
    """On the live graph `api.py` holds 111 `data_var` params, 29 functions and
    17 pipelines, and an alphabetical cut of 40 names is all params — so the
    read could not reach a single function. Excluding them would be SELECTION,
    which §10.3 forbids us; saying what is there and letting the reader narrow
    is folding. Both levers are printed."""
    doc = graph_view.unfold(graph, "app/core/load.py", depth=1, limit=2, ledger_conn=conn, project="proj")
    g = doc["groups"][0]
    assert g["node_types"] == [{"node_type": "function", "nodes": 2}, {"node_type": "data_var", "nodes": 1},
                               {"node_type": "file", "nodes": 1}]
    text = graph_view.render(doc)
    assert "function 2" in text and "data_var 1" in text
    # the exact command that shows all of them, plus the full set of kinds to narrow to
    assert "--limit 4" in text and "--type <function|data_var|file>" in text
    only = graph_view.unfold(graph, "app/core/load.py", depth=1, node_type="function", ledger_conn=conn, project="proj")
    assert [n["qualified_name"] for n in only["groups"][0]["names"]] == ["app.core.load.parse", "app.core.load.read"]
    assert only["node_type"] == "function" and only["groups"][0]["nodes"] == 2


def test_a_node_neighbourhood_can_be_narrowed_to_one_kind_too(conn, graph, seeded):
    doc = graph_view.unfold(graph, "app.core.load.parse", depth=1, node_type="function", ledger_conn=conn, project="proj")
    assert [n["qualified_name"] for n in doc["hops"][0]["neighbours"]] == ["app.core.load.read"]
    assert "read:param:src" not in graph_view.render(doc)      # the data_var neighbour is out, by the reader's choice
    assert doc["hops"][0]["node_types_available"] == [{"node_type": "data_var", "nodes": 1}, {"node_type": "function", "nodes": 1}]
    # the header's two numbers count different things, so they must say which:
    # `total` is deduped NAMES after --type, the breakdown is graph NODES before it
    assert doc["hops"][0]["total"] == 1 and doc["hops"][0]["nodes_at_hop"] == 2
    assert "1 names · of 2 graph nodes at this hop" in graph_view.render(doc)
    assert "kinds among those 2 nodes, before --type:" in graph_view.render(doc)


def test_one_name_is_one_line_in_a_path_group_too(conn, graph, seeded):
    """The node view collapses a repeated name; the path view must as well, or
    the two halves of one command disagree. Seen live with
    `--include-imports`: `(external)` printed `__future__` 195 times, because
    the analyzer records one module node per importing file."""
    g = sqlite3.connect(graph)
    g.executescript("""
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES
            (30, 2, 'read', 'app.core.load.read', 'app/core/load.py', 1, NULL),
            (31, 2, 'read', 'app.core.load.read', 'app/core/load.py', 1, NULL);
    """)
    g.commit(); g.close()
    doc = graph_view.unfold(graph, "app/core/load.py", depth=1, ledger_conn=conn, project="proj")
    names = doc["groups"][0]["names"]
    reads = [n for n in names if n["qualified_name"] == "app.core.load.read"]
    assert len(reads) == 1 and reads[0]["nodes"] == 3 and reads[0]["records"] == 2
    assert doc["groups"][0]["nodes"] == 6 and doc["groups"][0]["names_total"] == 4
    text = graph_view.render(doc)
    assert text.count("app.core.load.read ·") == 1 and "3 graph nodes" in text


# ── the CLI entry ────────────────────────────────────────────────────────────

def _cli(conn, *argv, env_extra=None):
    import os
    import subprocess
    repo = Path(__file__).resolve().parents[2]
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(repo / "orchestrator-backend"))
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv], capture_output=True, text=True,
                          env=env, cwd=str(repo))


@pytest.fixture
def registry(tmp_path, graph):
    reg = tmp_path / "projects.json"
    reg.write_text('{"projects": [{"name": "proj", "repo": "/x", "db_path": "%s", "commit_sha": "c"}]}' % graph)
    return {"PSG_REGISTRY_PATH": str(reg)}


def test_cli_graph_prints_the_root_view_then_unfolds_and_offers_json(conn, seeded, registry):
    root = _cli(conn, "graph", "--project", "proj", env_extra=registry)
    assert root.returncode == 0, root.stderr
    assert "graph folded to areas" in root.stdout and "provledger graph app/core --depth 1" in root.stdout
    area = _cli(conn, "graph", "app/core", "--depth", "1", "--project", "proj", env_extra=registry)
    assert area.returncode == 0 and "app.core.load.read" in area.stdout
    node = _cli(conn, "graph", "app.core.load.read", "--depth", "2", "--project", "proj", env_extra=registry)
    assert node.returncode == 0 and "calls" in node.stdout and "read:param:src" in node.stdout
    j = _cli(conn, "graph", "--project", "proj", "--json", env_extra=registry)
    assert j.returncode == 0 and '"areas"' in j.stdout and '"relations"' in j.stdout
    assert conn.execute("SELECT COUNT(*) FROM read_hit").fetchone()[0] == 0        # a read that writes nothing
    top = " ".join(_cli(conn, "--help").stdout.split())
    assert "graph" in top and "record" in top and "plan" in top
