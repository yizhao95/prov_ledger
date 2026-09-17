"""extract — where a number sits in a deck, a workbook, a report or a text file
(DP phase 4, Task 0).

Two rules make this module small on purpose.

**It only locates.** `extract` hands back `{locator, text}` pairs so that an
anchor can name a place and a later check can look at that same place. The text
it read is never written to the ledger — a deck's wording is the deck's, and a
provenance store that quietly accumulates other people's prose is a liability,
not a feature. The last test in this file is the one that says so.

**It never guesses.** An archive with no slides, a Word file with no document
part, a suffix nobody taught it — each raises `ExtractError` naming the file.
Returning an empty list for an unreadable file is the silent failure this whole
project exists to remove.

Zero dependencies: `zipfile` + `xml.etree` for the OOXML formats, `csv` for csv,
plain reads for markdown and text. The fixtures next door are built the same
way, by `make_fixtures.py`.
"""
from pathlib import Path

import pytest

from orchestrator.artifacts import extract as ex

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "artifacts"


def _by_text(entries, needle):
    return [e for e in entries if needle in e["text"]]


# ── pptx ─────────────────────────────────────────────────────────────────────

def test_a_deck_gives_slide_and_shape_for_every_piece_of_text():
    entries = ex.extract(FIXTURES / "deck_v1.pptx")
    assert entries[0]["locator"] == {"kind": "pptx", "slide": 1, "shape": 1}
    assert entries[0]["text"] == "Q3 business review"
    hit = _by_text(entries, "Q3 conv 3.2%")
    assert len(hit) == 1
    assert hit[0]["locator"] == {"kind": "pptx", "slide": 4, "shape": 2}


def test_slides_come_back_in_deck_order_not_in_zip_order():
    """`slide10.xml` sorts before `slide2.xml` as a string. A deck that renumbers
    its slides when it passes ten would make every anchor in it a lie."""
    slides = [e["locator"]["slide"] for e in ex.extract(FIXTURES / "deck_v1.pptx")]
    assert slides == sorted(slides)
    assert slides[0] == 1 and slides[-1] == 5


def test_the_second_version_of_the_deck_moved_the_number_to_another_slide():
    """The fixture pair the veto item is built on: same number, different place.
    Slide 4 keeps a headline and loses the figure."""
    v2 = ex.extract(FIXTURES / "deck_v2.pptx")
    assert _by_text(v2, "Q3 conv 3.2%")[0]["locator"] == {"kind": "pptx", "slide": 6, "shape": 2}
    slide4 = [e["text"] for e in v2 if e["locator"]["slide"] == 4]
    assert slide4 == ["Conversion", "see appendix"]


# ── xlsx ─────────────────────────────────────────────────────────────────────

def test_a_workbook_gives_the_sheet_name_and_the_cell_reference():
    entries = ex.extract(FIXTURES / "book.xlsx")
    cells = {(e["locator"]["sheet"], e["locator"]["cell"]): e["text"] for e in entries}
    assert cells[("Q3", "B7")] == "3.2"
    assert cells[("Q3", "B6")] == "64.33"
    assert all(e["locator"]["kind"] == "xlsx" for e in entries)


def test_a_workbook_resolves_shared_strings_and_inline_strings():
    """A cell that says `t="s"` holds an index into sharedStrings.xml, not a
    value. A reader that prints the index is reading a different document."""
    cells = {(e["locator"]["sheet"], e["locator"]["cell"]): e["text"] for e in ex.extract(FIXTURES / "book.xlsx")}
    assert cells[("Q3", "A7")] == "Q3 conv"            # shared string #1
    assert cells[("Q3", "C7")] == "signed off by finance"   # inline string
    assert cells[("Q3", "B1")] == "Value"                   # inline string in the header row


# ── docx ─────────────────────────────────────────────────────────────────────

def test_a_report_gives_one_entry_per_paragraph_in_order():
    entries = ex.extract(FIXTURES / "report.docx")
    assert [e["locator"] for e in entries[:2]] == [{"kind": "docx", "paragraph": 1}, {"kind": "docx", "paragraph": 2}]
    assert _by_text(entries, "Q3 conv 3.2%")[0]["locator"] == {"kind": "docx", "paragraph": 3}


# ── csv / markdown / text ────────────────────────────────────────────────────

def test_a_csv_gives_row_and_column():
    entries = ex.extract(FIXTURES / "table.csv")
    assert entries[0]["locator"] == {"kind": "csv", "row": 1, "col": 1} and entries[0]["text"] == "metric"
    hit = [e for e in entries if e["text"] == "3.2"]
    assert len(hit) == 1 and hit[0]["locator"] == {"kind": "csv", "row": 2, "col": 2}


def test_markdown_and_plain_text_give_line_numbers():
    md = ex.extract(FIXTURES / "notes.md")
    assert md[0]["locator"] == {"kind": "text", "line": 1} and md[0]["text"] == "# Q3 review"
    assert _by_text(md, "Q3 conv 3.2%")[0]["locator"] == {"kind": "text", "line": 3}
    txt = ex.extract(FIXTURES / "plain.txt")
    assert [e["locator"]["line"] for e in txt] == [1, 2, 3]


def test_blank_lines_keep_their_numbers_and_are_not_returned():
    """Line 2 of notes.md is empty. Dropping it silently would renumber every
    line below it — the fastest way to make a locator point somewhere else."""
    lines = [e["locator"]["line"] for e in ex.extract(FIXTURES / "notes.md")]
    assert 2 not in lines and lines == [1, 3, 5]


# ── never silent ─────────────────────────────────────────────────────────────

def test_an_archive_with_no_slides_raises_rather_than_returning_nothing():
    with pytest.raises(ex.ExtractError) as e:
        ex.extract(FIXTURES / "broken.pptx")
    assert "broken.pptx" in str(e.value)


def test_a_suffix_nobody_taught_it_raises_and_lists_what_it_knows(tmp_path):
    p = tmp_path / "slides.key"
    p.write_bytes(b"\x00\x01")
    with pytest.raises(ex.ExtractError) as e:
        ex.extract(p)
    assert "slides.key" in str(e.value) and "pptx" in str(e.value)


def test_a_file_that_is_not_there_raises_rather_than_returning_nothing(tmp_path):
    with pytest.raises(ex.ExtractError) as e:
        ex.extract(tmp_path / "absent.pptx")
    assert "absent.pptx" in str(e.value)


def test_a_docx_without_a_document_part_raises(tmp_path):
    import zipfile
    p = tmp_path / "empty.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "no document part here")
    with pytest.raises(ex.ExtractError):
        ex.extract(p)


# ── the text never lands in the ledger ───────────────────────────────────────

def test_extraction_writes_nothing_to_the_ledger(conn):
    """The module has no connection argument and no import of `db`, and this
    test makes that structural fact observable: every table in a migrated
    database holds exactly as many rows after a full extraction as before."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    for name in ("deck_v1.pptx", "deck_v2.pptx", "book.xlsx", "report.docx", "table.csv", "notes.md", "plain.txt"):
        assert ex.extract(FIXTURES / name)
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    assert before == after


def test_the_module_has_no_way_to_reach_a_database():
    import inspect
    src = inspect.getsource(ex)
    assert "sqlite3" not in src and "from . import db" not in src
    assert "conn" not in inspect.signature(ex.extract).parameters


# ── the fixtures are fixtures ────────────────────────────────────────────────

def test_the_committed_fixtures_are_what_the_generator_produces(tmp_path):
    """A re-run of make_fixtures.py must reproduce the committed bytes, or the
    fixtures have drifted from the script that documents them."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_fixtures", FIXTURES / "make_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name, path in mod.build(tmp_path).items():
        assert path.read_bytes() == (FIXTURES / name).read_bytes(), f"{name} has drifted from make_fixtures.py"
