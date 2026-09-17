"""anchor — a manual anchor that can be lost but never moved (DP phase 4, Task 1).

Spec §9. The deck fixtures next door are a pair on purpose: `deck_v1.pptx`
carries the Q3 conversion figure on slide 4, and in `deck_v2.pptx` somebody
moved that number to a new slide 6 and left a headline behind. That pair is the
whole of acceptance item F4, and `test_f4_a_lost_anchor_is_never_re_pointed` is
the veto test: `check` must say `anchor_lost`, and must not follow the number.

The rest of this file is the surrounding contract — a node has to exist before
anything can be a reading of it, a value has to really be at the place somebody
names, and auto-discovery stays off until somebody turns it on and even then
writes nothing.
"""
import json
import shutil
from pathlib import Path

import pytest

from orchestrator import provenance
from orchestrator.artifacts import anchor as an
from orchestrator.artifacts import extract as ex

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "artifacts"
PROJECT = "demo"


@pytest.fixture
def deck(tmp_path):
    """v1 in place, with v2 next to it ready to replace it."""
    path = tmp_path / "q3.pptx"
    shutil.copy(FIXTURES / "deck_v1.pptx", path)
    return path


def _metric(conn, name="q3_conv", value=3.2, project=PROJECT):
    conn.execute("INSERT INTO metrics (project, name, value, source) VALUES (?, ?, ?, 'record-metric')",
                 (project, name, value))
    conn.commit()


def _anchored(conn, deck, **over):
    kwargs = dict(project=PROJECT, file=deck, at="slide 4", node="metric:q3_conv", value="3.2")
    kwargs.update(over)
    return an.anchor(conn, **kwargs)


# ── the `--at` grammar ───────────────────────────────────────────────────────

def test_at_reads_the_five_ways_a_person_names_a_place():
    assert an.parse_at("slide 4") == {"kind": "pptx", "slide": 4}
    assert an.parse_at("slide 4 shape 2") == {"kind": "pptx", "slide": 4, "shape": 2}
    assert an.parse_at("Q3!B7") == {"kind": "xlsx", "sheet": "Q3", "cell": "B7"}
    assert an.parse_at("b7") == {"kind": "xlsx", "cell": "B7"}
    assert an.parse_at("paragraph 3") == {"kind": "docx", "paragraph": 3}
    assert an.parse_at("row 2 col 2") == {"kind": "csv", "row": 2, "col": 2}
    assert an.parse_at("line 3") == {"kind": "text", "line": 3}


def test_an_at_nobody_understands_is_refused_with_the_forms_that_work():
    with pytest.raises(an.AnchorError) as e:
        an.parse_at("somewhere near the top")
    assert "slide <n>" in str(e.value) and "paragraph <n>" in str(e.value)


def test_a_number_needs_a_boundary_before_it_counts_as_the_value():
    """`3.2` is not in `13.28`. An anchor that thought it was would report ok
    for ever while pointing at somebody else's figure."""
    assert an.holds_value("Q3 conv 3.2%", "3.2") is True
    assert an.holds_value("churn 13.28", "3.2") is False
    assert an.holds_value("conv 3.25", "3.2") is False


# ── anchoring ────────────────────────────────────────────────────────────────

def test_an_anchor_records_where_a_number_was_seen_and_what_it_is_a_reading_of(conn, deck):
    _metric(conn)
    out = _anchored(conn, deck)
    assert out["node_key"] == "metric:q3_conv"
    assert out["tier"] == "observed" and out["by"] == "human"
    assert out["value_text"] == "3.2" and out["value_num"] == 3.2
    assert out["locator"] == {"kind": "pptx", "slide": 4, "shape": 2, "at": "slide 4"}
    assert out["where"] == "slide 4 shape 2"
    # the file is named, and identified by its bytes — it is not the subject
    assert out["file"]["kind"] == "pptx" and out["file"]["sha256"] == an.sha256_of(deck)
    assert provenance.verify_chain(conn, "occurrence")["ok"] is True


def test_the_same_number_in_two_files_is_two_occurrences_of_one_node(conn, deck, tmp_path):
    """The file is where a number turned up. The node's history does not fork
    because somebody sent a second deck."""
    _metric(conn)
    other = tmp_path / "board_pack.pptx"
    shutil.copy(FIXTURES / "deck_v2.pptx", other)
    _anchored(conn, deck)
    _anchored(conn, other, at="slide 6")
    rows = an.for_node(conn, PROJECT, "metric:q3_conv")
    assert [r["where"] for r in rows] == ["slide 4 shape 2", "slide 6 shape 2"]
    assert len({r["file_path"] for r in rows}) == 2


def test_a_value_that_is_not_at_that_place_is_refused_with_what_is_there(conn, deck):
    _metric(conn)
    with pytest.raises(an.AnchorError) as e:
        _anchored(conn, deck, value="9.9")
    assert "9.9 is not at 'slide 4'" in str(e.value)
    assert "Q3 conv 3.2%" in str(e.value)          # the first 80 characters of what IS there
    assert conn.execute("SELECT COUNT(*) FROM occurrence").fetchone()[0] == 0


def test_a_place_that_is_not_in_the_file_is_refused_and_the_places_are_listed(conn, deck):
    _metric(conn)
    with pytest.raises(an.AnchorError) as e:
        _anchored(conn, deck, at="slide 9")
    assert "'slide 9' is not a place in q3.pptx" in str(e.value)
    assert "slide 1 shape 1" in str(e.value)


def test_an_anchor_may_only_point_at_a_node_that_already_exists(conn, deck):
    """A figure's identity is its data source. An anchor cannot invent one, and
    the refusal says which three shapes of name do exist."""
    with pytest.raises(an.AnchorError) as e:
        _anchored(conn, deck)                      # no metric recorded yet
    assert "metric:q3_conv is not a node of demo" in str(e.value)
    assert "metric:<name>" in str(e.value) and "<dataset>.<column>" in str(e.value)
    assert "declared:<slug>" in str(e.value)
    assert conn.execute("SELECT COUNT(*) FROM artifact_file").fetchone()[0] == 0


def test_a_declared_node_and_a_graph_column_are_both_anchorable(conn, deck, tmp_path):
    from orchestrator import declared
    row = declared.declare(conn, PROJECT, "Q3 conversion as finance computes it", node_type="manual_figure")
    uid = provenance.insert_utterance(conn, session_id="s", project=PROJECT, plan_id=None,
                                      text="finance computes it this way", occurred_at="2026-09-17 09:00:00")
    active = declared.confirm(conn, row["id"], uid)
    assert an.node_exists(conn, PROJECT, active["qualified_name"]) is True
    assert an.node_exists(conn, PROJECT, "orders.net_revenue", known_nodes={"orders.net_revenue"}) is True
    assert an.node_exists(conn, PROJECT, "orders.missing", known_nodes={"orders.net_revenue"}) is False


# ── checking ─────────────────────────────────────────────────────────────────

def test_a_check_that_finds_the_number_where_it_was_left_says_ok(conn, deck):
    _metric(conn)
    out = _anchored(conn, deck)
    verdict = an.check(conn, out["id"])
    assert verdict["state"] == "ok" and verdict["reason"] is None
    assert verdict["where"] == "slide 4" and verdict["value"] == "3.2"
    assert an.latest_state(conn, out["id"])["state"] == "ok"


@pytest.mark.veto
def test_f4_a_lost_anchor_is_never_re_pointed(conn, deck):
    """The veto item. v2 moved the number from slide 4 to slide 6. The anchor
    must report `anchor_lost`, name what was left behind, and leave its locator
    exactly where the person put it — no new occurrence, no silent re-aim."""
    _metric(conn)
    out = _anchored(conn, deck)
    an.check(conn, out["id"])                                  # ok on v1
    shutil.copy(FIXTURES / "deck_v2.pptx", deck)               # the revised deck lands at the same path
    verdict = an.check(conn, out["id"])

    assert verdict["state"] == "anchor_lost"
    assert "moved or removed" in verdict["reason"]
    assert verdict["reason"].startswith("3.2 is no longer at slide 4")
    assert verdict["found_instead"] and "see appendix" in verdict["found_instead"]

    # the number IS in the file — two slides later — and nothing followed it
    moved = [e for e in ex.extract(deck) if "3.2" in e["text"]]
    assert moved and moved[0]["locator"]["slide"] == 6
    assert json.loads(an.get(conn, out["id"])["locator_json"])["slide"] == 4
    assert conn.execute("SELECT COUNT(*) FROM occurrence").fetchone()[0] == 1
    assert [r[0] for r in conn.execute("SELECT state FROM anchor_state ORDER BY id")] == ["ok", "anchor_lost"]
    assert an.lost_count(conn, PROJECT) == 1


def test_a_file_that_is_gone_is_a_lost_anchor_naming_the_path(conn, deck):
    _metric(conn)
    out = _anchored(conn, deck)
    deck.unlink()
    verdict = an.check(conn, out["id"])
    assert verdict["state"] == "anchor_lost" and "the file is no longer at" in verdict["reason"]
    assert str(deck) in verdict["reason"]


def test_a_locator_that_no_longer_exists_is_a_lost_anchor(conn, tmp_path):
    """Anchored on line 5 of a note; the note is rewritten down to two lines.
    The place itself is gone, which is a different sentence from "the value
    changed", and the reason says so."""
    _metric(conn, name="net_revenue", value=64.33)
    note = tmp_path / "notes.md"
    shutil.copy(FIXTURES / "notes.md", note)
    out = an.anchor(conn, PROJECT, note, "line 5", "metric:net_revenue", "64.33")
    note.write_text("# Q3 review\n\nnothing else to say\n", encoding="utf-8")
    verdict = an.check(conn, out["id"])
    assert verdict["state"] == "anchor_lost"
    assert verdict["reason"] == "line 5 is not in this file any more"


def test_every_check_is_appended_so_a_verdict_has_a_date_and_a_reason(conn, deck):
    _metric(conn)
    out = _anchored(conn, deck)
    an.check(conn, out["id"])
    shutil.copy(FIXTURES / "deck_v2.pptx", deck)
    an.check(conn, out["id"])
    shutil.copy(FIXTURES / "deck_v1.pptx", deck)
    an.check(conn, out["id"])
    rows = [dict(r) for r in conn.execute("SELECT state, reason, checked_at FROM anchor_state ORDER BY id")]
    assert [r["state"] for r in rows] == ["ok", "anchor_lost", "ok"]
    assert rows[1]["reason"] and all(len(r["checked_at"]) == 19 for r in rows)


def test_checking_a_whole_project_reports_one_line_per_anchor(conn, deck, tmp_path):
    _metric(conn)
    _metric(conn, name="net_revenue", value=64.33)
    _anchored(conn, deck)
    an.anchor(conn, PROJECT, deck, "slide 2", "metric:net_revenue", "64.33")
    shutil.copy(FIXTURES / "deck_v2.pptx", deck)      # v2 keeps net revenue on slide 2, moves the conversion figure
    out = an.check_project(conn, PROJECT)
    assert [(o["node_key"], o["state"]) for o in out] == [("metric:q3_conv", "anchor_lost"),
                                                          ("metric:net_revenue", "ok")]
    assert an.lost_count(conn, PROJECT) == 1


# ── auto-discovery, which is off ─────────────────────────────────────────────

def test_candidates_are_off_by_default_and_the_refusal_says_how_to_turn_them_on(conn, deck):
    _metric(conn)
    with pytest.raises(an.AnchorError) as e:
        an.candidates(conn, PROJECT, deck)
    assert "off by default" in str(e.value) and "auto_discover" in str(e.value)


def test_candidates_when_switched_on_suggest_and_write_nothing(conn, deck):
    """Even switched on this only proposes: tier `asserted`, no occurrence, and
    a command for the person to run. `observed` means a person looked."""
    _metric(conn)
    out = an.candidates(conn, PROJECT, deck, enabled=True)
    assert [c["where"] for c in out] == ["slide 4 shape 2"]
    assert out[0]["node_key"] == "metric:q3_conv" and out[0]["tier"] == "asserted"
    assert out[0]["anchor_with"].startswith("provledger anchor")
    assert conn.execute("SELECT COUNT(*) FROM occurrence").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM artifact_file").fetchone()[0] == 0


def test_candidates_only_offer_numbers_this_project_has_actually_recorded(conn, deck):
    """64.33 is on slide 2 of the deck, but no metric of this project says it,
    so it is not a candidate. The intersection is the whole point."""
    _metric(conn)
    values = {c["value"] for c in an.candidates(conn, PROJECT, deck, enabled=True)}
    assert values == {"3.2"}


# ── the anchor is the place the person named ─────────────────────────────────

def test_a_check_honours_the_place_the_person_named_not_the_one_it_resolved_to(conn, tmp_path):
    """Found while smoking the CLI. The occurrence records where the number was
    seen — slide 4, shape 2 — but what the person created is the pointer they
    typed: `--at "slide 4"`. So a deck that gains a text box above the figure,
    shifting every shape index on that slide, must NOT lose an anchor scoped to
    the slide. Somebody who scoped theirs to `slide 4 shape 2` asked a narrower
    question and gets the narrower answer.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_fixtures", FIXTURES / "make_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    deck = tmp_path / "q3.pptx"
    mod._deck(deck, mod.DECK_V1)
    _metric(conn)
    loose = an.anchor(conn, PROJECT, deck, "slide 4", "metric:q3_conv", "3.2")
    tight = an.anchor(conn, PROJECT, deck, "slide 4 shape 2", "metric:q3_conv", "3.2")

    shifted = [list(s) for s in mod.DECK_V1]
    shifted[3].insert(0, ["a note somebody added above the headline"])   # slide 4 gains a shape
    mod._deck(deck, shifted)

    assert an.check(conn, loose["id"])["state"] == "ok"
    lost = an.check(conn, tight["id"])
    assert lost["state"] == "anchor_lost" and "moved or removed" in lost["reason"]
