"""The fact table says what an agent used to guess (skill redesign, 2026-10-06).

Real /ledger and /receipts sessions filled two gaps the fact table left:

  · a node whose reason slot was closed `unstated` printed only `reasons (0)`,
    which reads the same as "never touched"; the agent took the goal of the task
    that changed it, or a neighbour's reason, and presented it as the node's;
  · a source printed as `source [#r1] email · <label> · <uri>`, with nothing
    saying the ledger holds only the pointer and nobody ever checked it; the
    agent called the email "attached" and offered to forward it.

So the table now prints both, and opens with a legend: the tiers and the cite
tokens are defined once, in code, and arrive exactly when an agent reads a table.
"""
import sys
from pathlib import Path

import pytest

from orchestrator import provenance as pv
from orchestrator.ask import absence as A, facts as F, summarize as SUM

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402


@pytest.fixture
def graph(tmp_path):
    """Two pipeline nodes, both changed in run 2."""
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    for key, qn in (("nk_a", "pkg.pipe.load"), ("nk_b", "pkg.pipe.rate")):
        ps.add_snapshot(c, 1, key, qn)
        ps.add_event(c, 1, 1 if key == "nk_a" else 2, "node_added", key, created_at="2026-09-01T09:00:00+00:00")
    ps.add_run(c, 2, plan_id="P1")
    for i, (key, qn) in enumerate((("nk_a", "pkg.pipe.load"), ("nk_b", "pkg.pipe.rate")), start=1):
        ps.add_snapshot(c, 2, key, qn, struct_sig="s2")
        ps.add_event(c, 2, i, "signature_changed", key, '{"struct_sig": "s2"}', created_at="2026-09-10T09:00:00+00:00")
    c.commit(); c.close()
    return str(path)


@pytest.fixture
def seeded(conn):
    """pkg.pipe.load: changed by P1, closed with nobody saying why.
    pkg.pipe.rate: a reason in a person's own words, resting on a linked email."""
    ids = {}
    ids["unstated"] = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical",
                                       recorded_by="system", occurred_at="2026-09-10 12:00:00")
    text = "the feed drops the discount column, so the rollup has to stop reading it"
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P1", text=text,
                            occurred_at="2026-09-01 10:00:00")
    ids["stated"] = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_b", kind="organizational",
                                     verbatim=(u, 0, len(text)), recorded_by="human",
                                     occurred_at="2026-09-01 10:00:00")
    ids["email"] = pv.insert_reference(conn, project="proj", kind="email", label="Re: feed drops discount",
                                       uri="mailto:data@example.invalid", occurred_at="2026-09-01 09:30:00")
    pv.link_reference(conn, ids["stated"], ids["email"])
    conn.commit()
    return ids


def _table(conn, graph):
    return F.facts(conn, graph, ["pkg.pipe.load", "pkg.pipe.rate"], project="proj")


def _node(ft, qn):
    return next(n for n in ft["nodes"] if n["qn"] == qn)


def _block(text: str, qn: str) -> str:
    start = text.index(f"## {qn} ")
    end = text.find("\n## ", start + 1)
    return text[start:end if end > 0 else None]


def test_a_reason_closed_unstated_is_a_line_of_its_own_with_an_id(conn, graph, seeded):
    ft = _table(conn, graph)
    block = _block(F.render(ft), "pkg.pipe.load")
    assert "reasons (1)" in block, "an unstated slot is a recorded fact, not nothing"
    line = next(l for l in block.splitlines() if f"[#{seeded['unstated']}]" in l)
    assert "unstated" in line and "2026-09-10" in line and "plan P1" in line
    assert "nobody said why this changed" in block


def test_a_node_whose_reason_was_never_given_has_an_absence_saying_so(conn, graph, seeded):
    ft = _table(conn, graph)
    said = [a["text"] for a in A.absences(conn, ft) if a["code"] == "no_reason_recorded"]
    assert said == ["No reason was recorded for `pkg.pipe.load`. [scope]"]


def test_a_source_says_it_is_only_a_link_and_whether_anyone_checked_it(conn, graph, seeded):
    block = _block(F.render(_table(conn, graph)), "pkg.pipe.rate")
    line = next(l for l in block.splitlines() if l.strip().startswith(f"source [#r{seeded['email']}]"))
    assert "link only — the body is not in the ledger" in line
    assert "never checked" in line


def test_a_checked_source_says_when(conn, graph, seeded):
    conn.execute("UPDATE reference SET last_checked = '2026-09-20 08:00:00' WHERE id = ?", (seeded["email"],))
    conn.commit()
    block = _block(F.render(_table(conn, graph)), "pkg.pipe.rate")
    line = next(l for l in block.splitlines() if l.strip().startswith(f"source [#r{seeded['email']}]"))
    assert "last checked 2026-09-20" in line and "never checked" not in line


def test_the_table_opens_with_the_legend(conn, graph, seeded):
    head = F.render(_table(conn, graph)).split("\n## ", 1)[0]
    for tier in ("stated", "asserted", "derived", "observed", "unstated"):
        assert f"\n  {tier} " in head, tier
    for token in ("[#N]", "[#rN]", "[#iN]", "[#eN]", "[#xN]", "[#oN]", "[#mN]", "[scope]"):
        assert token in head, token
    assert F.LEGEND in head, "one legend, defined once in code"
    assert not F.numbers_in(F.LEGEND), "a digit in the legend would become a number every answer may use"


def test_a_sentence_citing_the_unstated_record_survives_the_check(conn, graph, seeded):
    ft = _table(conn, graph)
    draft = f"pkg.pipe.load was changed on 2026-09-10 and nobody said why [#{seeded['unstated']}]."
    out = SUM.review(draft, ft)
    assert out["sentences"] == [draft]


def test_the_legend_says_how_to_put_each_record_into_words():
    """One place for what a record is and how to say it: both skills read the
    legend instead of each carrying a copy of the same rules."""
    legend = " ".join(F.LEGEND.split())
    assert "<who> said on <date>" in legend, "stated: a person's words, given as theirs, with the day"
    assert "recorded as the understanding" in legend and "not an agreement" in legend
    assert "nobody said why" in legend
    assert "the goal of the task that changed it is the task's" in legend
    assert "never its body" in legend and "never checked" in legend
    assert "word for word" in legend, "an absence is reproduced, never rewritten"
