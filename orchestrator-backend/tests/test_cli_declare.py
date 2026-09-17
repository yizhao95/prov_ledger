"""`provledger node` — the entry point for the third core (DP phase 2c, spec §18).

One sentence goes in; a draft comes back with the confirmation line printed
next to it. Nothing enters the graph until the user says the words that put it
there, and those words are recorded as an utterance so the node's tier has a
span to point at.

A `business_rule` or a `stakeholder_decision` that constrains something is also
a CONSTRAINT: §18 calls it "the node form of a constraint", so confirming one
anchors an active constraint on each node it names. That is what makes it reach
the next plan's headline.
"""
from __future__ import annotations

import json

import pytest

from orchestrator import cli, db, provenance

KNOWN = ("pkg.rollup.weekly_report", "pkg.rollup.load_orders")


@pytest.fixture
def declared():
    from orchestrator import declared as mod
    return mod


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An orchestrator DB plus a registry whose project has two nodes."""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    import _psg_schema as ps
    dbp = tmp_path / "orch.db"
    conn = db.open_db(dbp)
    db.run_migrations(conn)
    conn.close()
    graph = tmp_path / "demo-state-graph.db"
    c = ps.build(graph)
    ps.add_run(c, 1, plan_id="P0", sha="aaaaaaa")
    for i, qn in enumerate(KNOWN, start=1):
        ps.add_snapshot(c, 1, f"nk_{i}", qn)
    c.commit()
    c.close()
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "demo", "repo": str(tmp_path), "db_path": str(graph),
                                             "commit_sha": "aaaaaaa"}]}))
    monkeypatch.setenv("ORCH_DB", str(dbp))
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    return dbp


def _run(*argv) -> int:
    return cli.main(list(argv))


def _out(capsys) -> dict:
    """The last JSON object the CLI printed (the output is indented, so a
    line-based read would only ever see a closing brace)."""
    text = capsys.readouterr().out
    decoder, idx, last = json.JSONDecoder(), 0, None
    while True:
        start = text.find("{", idx)
        if start < 0:
            break
        try:
            doc, end = decoder.raw_decode(text, start)
        except ValueError:
            idx = start + 1
            continue
        last, idx = doc, end
    assert last is not None, f"no JSON in CLI output: {text!r}"
    return last


def test_declare_prints_a_draft_and_writes_nothing_active(repo, capsys, declared):
    assert _run("node", "declare", "EMEA excluded from the Q3 rollup", "--type", "business_rule",
                "--links-to", "pkg.rollup.weekly_report", "--attr", "decided_on=2026-03-14",
                "--project", "demo", "--json") == 0
    out = _out(capsys)
    assert out["state"] == "draft" and out["tier"] == "stated"
    assert out["qualified_name"] == "declared:emea-excluded-from-the-q3-rollup"
    assert out["confirm_with"].startswith("provledger node declare --confirm ")
    conn = db.open_db(repo)
    assert declared.active(conn, "demo") == []
    conn.close()


def test_declare_refuses_a_link_that_is_not_in_the_graph(repo, capsys):
    assert _run("node", "declare", "EMEA excluded", "--type", "business_rule",
                "--links-to", "pkg.rollup.does_not_exist", "--project", "demo") == 2
    assert "pkg.rollup.does_not_exist" in capsys.readouterr().err


def test_confirming_records_the_words_and_activates_the_node(repo, capsys, declared):
    _run("node", "declare", "EMEA excluded from the Q3 rollup", "--type", "business_rule",
         "--links-to", "pkg.rollup.weekly_report", "--project", "demo", "--json")
    draft = _out(capsys)["id"]
    words = "EMEA is excluded from the Q3 rollup — the steering group decided on 2026-03-14"
    assert _run("node", "declare", "--confirm", str(draft), "--words", words,
                "--at", "2026-03-14 09:00", "--project", "demo", "--json") == 0
    out = _out(capsys)
    assert out["state"] == "active" and out["tier"] == "stated"
    conn = db.open_db(repo)
    try:
        rows = declared.active(conn, "demo")
        assert len(rows) == 1 and rows[0]["id"] == out["id"]
        u = provenance.get_utterance(conn, rows[0]["description_utterance_id"])
        assert u["text"] == words and u["occurred_at"].startswith("2026-03-14 09:00")
    finally:
        conn.close()


def test_a_confirmed_business_rule_also_anchors_a_constraint_on_what_it_constrains(repo, capsys):
    """Spec §18: a rule is the node form of a constraint — it is both a node in
    the graph and a record the next plan's headline can find."""
    _run("node", "declare", "EMEA excluded from the Q3 rollup", "--type", "business_rule",
         "--links-to", "pkg.rollup.weekly_report", "--project", "demo", "--json")
    draft = _out(capsys)["id"]
    _run("node", "declare", "--confirm", str(draft), "--words", "EMEA is excluded from the Q3 rollup",
         "--at", "2026-03-14 09:00", "--project", "demo", "--json")
    out = _out(capsys)
    conn = db.open_db(repo)
    try:
        rows = conn.execute("SELECT node_key, role, state, tier, statement FROM change_reason_v "
                            "WHERE project = 'demo' AND role = 'constraint'").fetchall()
        assert len(rows) == 1
        r = dict(rows[0])
        assert r["node_key"] in ("nk_1", "pkg.rollup.weekly_report")
        assert r["state"] == "active" and r["tier"] == "stated"
        assert "EMEA" in r["statement"]
    finally:
        conn.close()
    assert out["constraints_anchored"] == 1


def test_an_external_system_anchors_no_constraint(repo, capsys):
    _run("node", "declare", "Salesforce export feed", "--type", "external_system",
         "--links-to", "pkg.rollup.load_orders", "--project", "demo", "--json")
    draft = _out(capsys)["id"]
    _run("node", "declare", "--confirm", str(draft), "--words", "orders come from the Salesforce export",
         "--at", "2026-03-14 09:00", "--project", "demo", "--json")
    assert _out(capsys)["constraints_anchored"] == 0


def test_list_show_and_retire_round_trip(repo, capsys, declared):
    _run("node", "declare", "EMEA excluded from the Q3 rollup", "--type", "business_rule",
         "--project", "demo", "--json")
    draft = _out(capsys)["id"]
    _run("node", "declare", "--confirm", str(draft), "--words", "EMEA is excluded",
         "--at", "2026-03-14 09:00", "--project", "demo", "--json")
    slug = _out(capsys)["slug"]
    assert _run("node", "list", "--project", "demo", "--json") == 0
    assert [r["slug"] for r in _out(capsys)["nodes"]] == [slug]
    assert _run("node", "show", slug, "--project", "demo", "--json") == 0
    shown = _out(capsys)
    assert shown["slug"] == slug and len(shown["versions"]) == 2
    assert shown["versions"][0]["state"] == "draft" and shown["versions"][1]["state"] == "active"
    assert _run("node", "retire", slug, "--project", "demo", "--json") == 0
    assert _out(capsys)["state"] == "retired"
    assert _run("node", "list", "--project", "demo", "--json") == 0
    assert _out(capsys)["nodes"] == []


def test_show_says_so_when_there_is_no_such_declaration(repo, capsys):
    assert _run("node", "show", "no-such-rule", "--project", "demo") == 1
    assert "no-such-rule" in capsys.readouterr().err
