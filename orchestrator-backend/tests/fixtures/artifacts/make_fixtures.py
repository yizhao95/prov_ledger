"""Build the committed artifact fixtures with the standard library alone.

A deck, a workbook and a report are the files the numbers in this project
actually live in, so the suite needs real ones. `python-pptx` / `openpyxl`
would be three dependencies for three zip archives, and this project ships
none — so the archives are written here by hand, with `zipfile` and string
templates, and the results are committed next to this file.

Run it from anywhere:

    python orchestrator-backend/tests/fixtures/artifacts/make_fixtures.py

It is deterministic (fixed timestamps inside the archives), so a re-run leaves
the committed bytes alone. `test_extract.py` asserts the committed files still
say what the tests expect, which is what makes them fixtures rather than
scratch files.

The deck comes in two versions on purpose. `deck_v1.pptx` carries the Q3
conversion figure on slide 4; in `deck_v2.pptx` somebody moved that slide's
number to slide 6 and left the headline behind. That pair is the whole of
DP phase 4's veto item: an anchor on slide 4 must report `anchor_lost`, and
must NOT quietly follow the number to slide 6.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXED_TIME = (2026, 9, 17, 0, 0, 0)     # deterministic archives — a re-run rewrites the same bytes

# ── OOXML fragments ──────────────────────────────────────────────────────────

_SLIDE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>{shapes}</p:spTree></p:cSld>
</p:sld>
"""
_SHAPE = """
    <p:sp><p:nvSpPr><p:cNvPr id="{n}" name="TextBox {n}"/></p:nvSpPr>
      <p:txBody>{paras}</p:txBody></p:sp>"""
_PARA = """<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"""

_PRESENTATION = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldIdLst>{ids}</p:sldIdLst>
</p:presentation>
"""

_DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{paras}</w:body>
</w:document>
"""
_W_PARA = """
    <w:p><w:r><w:t>{text}</w:t></w:r></w:p>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>{sheets}</sheets>
</workbook>
"""
_WB_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>
"""
_SHARED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{n}" uniqueCount="{n}">{items}</sst>
"""
_SHEET = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>{rows}</sheetData>
</worksheet>
"""


def _slide(shapes: list[list[str]]) -> str:
    out = []
    for i, paras in enumerate(shapes, start=1):
        out.append(_SHAPE.format(n=i, paras="".join(_PARA.format(text=t) for t in paras)))
    return _SLIDE.format(shapes="".join(out))


def _deck(path: Path, slides: list[list[list[str]]]) -> None:
    ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in range(len(slides)))
    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="ppt/presentation.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
            '</Relationships>',
        "ppt/presentation.xml": _PRESENTATION.format(ids=ids),
    }
    for i, shapes in enumerate(slides, start=1):
        files[f"ppt/slides/slide{i}.xml"] = _slide(shapes)
    _write_zip(path, files)


def _docx(path: Path, paragraphs: list[str]) -> None:
    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="word/document.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
            '</Relationships>',
        "word/document.xml": _DOCUMENT.format(paras="".join(_W_PARA.format(text=t) for t in paragraphs)),
    }
    _write_zip(path, files)


def _xlsx(path: Path) -> None:
    """One sheet named `Q3` (not `Sheet1`, so the workbook relationship is
    genuinely exercised), a shared string, an inline string and two numbers."""
    shared = ["Metric", "Q3 conv", "Net revenue"]
    rows = [
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>Value</t></is></c></row>',
        '<row r="6"><c r="A6" t="s"><v>2</v></c><c r="B6"><v>64.33</v></c></row>',
        '<row r="7"><c r="A7" t="s"><v>1</v></c><c r="B7"><v>3.2</v></c>'
        '<c r="C7" t="inlineStr"><is><t>signed off by finance</t></is></c></row>',
    ]
    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="xl/workbook.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
            '</Relationships>',
        "xl/workbook.xml": _WORKBOOK.format(sheets='<sheet name="Q3" sheetId="1" r:id="rId1"/>'),
        "xl/_rels/workbook.xml.rels": _WB_RELS.format(
            rels='<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
                 'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
                 '<Relationship Id="rId2" Target="sharedStrings.xml" '
                 'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"/>'),
        "xl/sharedStrings.xml": _SHARED.format(n=len(shared),
                                               items="".join(f"<si><t>{s}</t></si>" for s in shared)),
        "xl/worksheets/sheet1.xml": _SHEET.format(rows="".join(rows)),
    }
    _write_zip(path, files)


def _write_zip(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, body)


# ── the fixtures ─────────────────────────────────────────────────────────────

# v1: the Q3 conversion figure is on slide 4; net revenue is on slide 2.
DECK_V1 = [
    [["Q3 business review"], ["prepared for the steering group"]],
    [["Revenue"], ["Net revenue 64.33"]],
    [["Funnel"], ["visits, signups, paid"]],
    [["Conversion"], ["Q3 conv 3.2%"]],
    [["Next quarter"], ["hiring plan"]],
]
# v2: somebody moved the number to a new slide 6 and left the headline on 4.
DECK_V2 = [
    [["Q3 business review"], ["prepared for the steering group"]],
    [["Revenue"], ["Net revenue 64.33"]],
    [["Funnel"], ["visits, signups, paid"]],
    [["Conversion"], ["see appendix"]],
    [["Next quarter"], ["hiring plan"]],
    [["Appendix: conversion"], ["Q3 conv 3.2%"]],
]
DOCX_PARAGRAPHS = [
    "Q3 business review",
    "The funnel held through the quarter.",
    "Q3 conv 3.2% against a 3.0% plan.",
    "Net revenue 64.33 for the quarter.",
]
CSV_TEXT = "metric,value,unit\nq3_conv,3.2,percent\nnet_revenue,64.33,usd_m\n"
MD_TEXT = ("# Q3 review\n"
           "\n"
           "Conversion landed at Q3 conv 3.2% for the quarter.\n"
           "\n"
           "Net revenue 64.33 against a 60.00 plan.\n")
TXT_TEXT = ("q3 notes\n"
            "conv 3.2\n"
            "revenue 64.33\n")


def build(out_dir: Path | None = None) -> dict[str, Path]:
    out = Path(out_dir or HERE)
    out.mkdir(parents=True, exist_ok=True)
    made: dict[str, Path] = {}
    _deck(out / "deck_v1.pptx", DECK_V1);       made["deck_v1.pptx"] = out / "deck_v1.pptx"
    _deck(out / "deck_v2.pptx", DECK_V2);       made["deck_v2.pptx"] = out / "deck_v2.pptx"
    _xlsx(out / "book.xlsx");                   made["book.xlsx"] = out / "book.xlsx"
    _docx(out / "report.docx", DOCX_PARAGRAPHS); made["report.docx"] = out / "report.docx"
    (out / "table.csv").write_text(CSV_TEXT, encoding="utf-8");  made["table.csv"] = out / "table.csv"
    (out / "notes.md").write_text(MD_TEXT, encoding="utf-8");    made["notes.md"] = out / "notes.md"
    (out / "plain.txt").write_text(TXT_TEXT, encoding="utf-8");  made["plain.txt"] = out / "plain.txt"
    # A zip that is not a deck: the extractor must say so rather than return nothing.
    _write_zip(out / "broken.pptx", {"hello.txt": "this archive holds no slides"})
    made["broken.pptx"] = out / "broken.pptx"
    return made


if __name__ == "__main__":
    for name, path in build().items():
        print(f"{name:16} {path.stat().st_size:6} bytes")
