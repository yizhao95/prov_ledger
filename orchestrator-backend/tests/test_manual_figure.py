"""`provledger node add --manual-figure` — a number with no traceable source
(DP phase 4, Task 2; spec §9 and §18).

Some figures in a deck come from nowhere the machine can reach: somebody did
the arithmetic in their head, or in a spreadsheet nobody kept. The honest move
is not to refuse them and not to dress them up as metrics, but to let them into
the same graph with the truth attached: node type `manual_figure`, tier
`stated` because the person typed the number, and the words "no traceable data
source" on every surface that shows it. Selfcheck then counts what share of
this project's figures are of that kind.

It reuses the 2c declared path wholesale — `manual_figure` was already one of
its five node types. What this task adds is the name (the slug is the figure's
name, not a slug of the sentence), the value as an attribute, and the one-step
path from typing the number to an active, stated node.
"""
from __future__ import annotations

import json

import pytest

import sys
from pathlib import Path

from orchestrator import cli, db

# orchestrator-backend/tests is not a package: siblings are imported through the
# directory on sys.path, exactly as test_cli_declare imports _psg_schema.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cli_declare import _out, repo  # noqa: E402,F401 — the fixture is the point


@pytest.fixture
def declared():
    from orchestrator import declared as mod
    return mod


def _run(*argv) -> int:
    return cli.main(list(argv))


def test_a_manual_figure_is_a_declared_node_that_says_where_it_came_from(repo, capsys, declared):  # noqa: F811
    assert _run("node", "add", "--manual-figure", "q3_conv_manual", "--value", "3.2",
                "--note", "finance worked it out from the raw export; the pipeline never saw it",
                "--project", "demo", "--json") == 0
    out = _out(capsys)
    assert out["node_type"] == "manual_figure"
    assert out["qualified_name"] == "declared:q3-conv-manual"     # the figure's name, not a slug of the sentence
    assert out["attrs"] == {"note": "finance worked it out from the raw export; the pipeline never saw it",
                            "value": "3.2"}
    assert out["tier"] == "stated" and out["state"] == "active"   # the person typed the number
    assert out["traceable_source"] is False
    conn = db.open_db(repo)
    rows = declared.active(conn, "demo")
    assert [r["qualified_name"] for r in rows] == ["declared:q3-conv-manual"]
    assert json.loads(rows[0]["field_tiers_json"])["attrs"]["value"] == "stated"
    assert rows[0]["description_utterance_id"] is not None        # the words that put it in the graph
    conn.close()


def test_the_words_the_person_typed_are_what_makes_it_stated(repo, capsys):  # noqa: F811
    from orchestrator import provenance
    _run("node", "add", "--manual-figure", "q3_conv_manual", "--value", "3.2",
         "--note", "finance worked it out from the raw export", "--project", "demo", "--json")
    out = _out(capsys)
    conn = db.open_db(repo)
    said = provenance.get_utterance(conn, out["utterance_id"])
    assert said["text"] == "finance worked it out from the raw export"
    conn.close()


def test_without_the_words_nothing_enters_the_graph_and_the_line_to_run_is_printed(repo, capsys, declared):  # noqa: F811
    """The 2c rule holds here too: a node becomes active when somebody says the
    sentence that puts it there. Without `--note` this stays a draft, loudly."""
    assert _run("node", "add", "--manual-figure", "q3_conv_manual", "--value", "3.2",
                "--project", "demo", "--json") == 0
    out = _out(capsys)
    assert out["state"] == "draft" and out["utterance_id"] is None
    assert out["confirm_with"].startswith("provledger node declare --confirm ")
    conn = db.open_db(repo)
    assert declared.active(conn, "demo") == []
    conn.close()


def test_the_printed_line_says_the_number_has_no_traceable_source(repo, capsys):  # noqa: F811
    assert _run("node", "add", "--manual-figure", "q3_conv_manual", "--value", "3.2",
                "--note", "finance worked it out", "--project", "demo") == 0
    text = capsys.readouterr().out
    assert "declared:q3-conv-manual = 3.2" in text
    assert "no traceable data source" in text
    assert "manual_figure" in text and "tier stated" in text


def test_a_manual_figure_needs_a_name_and_a_number(repo, capsys):  # noqa: F811
    assert _run("node", "add", "--manual-figure", "q3_conv_manual", "--project", "demo") == 2
    assert "--value" in capsys.readouterr().err
    assert _run("node", "add", "--value", "3.2", "--project", "demo") == 2
    assert "--manual-figure" in capsys.readouterr().err


def test_a_manual_figure_can_be_anchored_like_any_other_node(repo, capsys):  # noqa: F811
    """The point of putting it in the graph: from here on it is a node like the
    others, so a deck that prints it can be anchored to it."""
    import shutil
    from pathlib import Path

    from orchestrator.artifacts import anchor as an
    fixtures = Path(__file__).resolve().parent / "fixtures" / "artifacts"
    _run("node", "add", "--manual-figure", "q3_conv_manual", "--value", "3.2",
         "--note", "finance worked it out", "--project", "demo", "--json")
    deck = Path(str(repo)).parent / "q3.pptx"
    shutil.copy(fixtures / "deck_v1.pptx", deck)
    conn = db.open_db(repo)
    row = an.anchor(conn, "demo", deck, "slide 4", "declared:q3-conv-manual", "3.2")
    assert row["node_key"] == "declared:q3-conv-manual" and row["tier"] == "observed"
    conn.close()
