"""Where a piece of text sits inside a deck, a workbook, a report or a text file.

DP phase 4, spec §9. Two rules keep this module small, and both of them are
load-bearing.

**It only locates.** `extract(path)` returns `{locator, text}` pairs so that a
person can pin one of those places to a node, and so a later check can look at
that same place again. What it read is never stored: a deck's wording belongs
to the deck, and a provenance store that quietly accumulates other people's
prose has become a copy of the documents it was meant to point at. There is no
connection parameter here and no import that could reach a database.

**It never guesses.** An archive with no slides, a Word file with no document
part, a suffix nobody taught it, a file that is not there — each raises
`ExtractError` naming the file. An empty list would read as "this file says
nothing", which is a different and much more dangerous statement.

Locators, all 1-based:

    {kind: "pptx", slide: n, shape: n}
    {kind: "xlsx", sheet: "<name>", cell: "B7"}
    {kind: "docx", paragraph: n}
    {kind: "csv",  row: n, col: n}
    {kind: "text", line: n}

Zero dependencies: `zipfile` and `xml.etree` for the OOXML containers, `csv`
for csv, plain reads for markdown and text.
"""
from __future__ import annotations

import csv as _csv
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

#: file suffix → the `kind` recorded on artifact_file
KINDS: dict[str, str] = {".pptx": "pptx", ".xlsx": "xlsx", ".docx": "docx", ".csv": "csv",
                         ".md": "text", ".markdown": "text", ".txt": "text", ".rst": "text"}

_SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml$")
_SHEET_RE = re.compile(r"xl/worksheets/sheet(\d+)\.xml$")
_CELL_RE = re.compile(r"^([A-Z]+)(\d+)$")
_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class ExtractError(Exception):
    """A file this module cannot read — named, never silently skipped."""


# ── helpers ──────────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    """The tag without its namespace. OOXML files in the wild bind the same
    namespaces to different prefixes, so matching on the local name is the only
    reading that survives contact with a real deck."""
    return tag.rpartition("}")[2]


def _text_of(node, *, joiner: str = "") -> str:
    """Every `<a:t>` / `<w:t>` / `<t>` under this element, in document order."""
    return joiner.join(t.text or "" for t in node.iter() if _local(t.tag) == "t")


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ExtractError(f"{path.name} is not a readable archive: {e}") from e


def _parse(z: zipfile.ZipFile, name: str, path: Path):
    try:
        return ET.fromstring(z.read(name))
    except KeyError as e:
        raise ExtractError(f"{path.name} has no {name}") from e
    except ET.ParseError as e:
        raise ExtractError(f"{path.name}: {name} is not well-formed XML: {e}") from e


# ── pptx ─────────────────────────────────────────────────────────────────────

def _extract_pptx(path: Path) -> list[dict]:
    with _open_zip(path) as z:
        slides = sorted(((int(m.group(1)), m.group(0)) for m in
                         (_SLIDE_RE.search(n) for n in z.namelist()) if m))
        # slide10 sorts before slide2 as a string; a deck that renumbers itself
        # past ten would make every anchor in it point at the wrong page.
        if not slides:
            raise ExtractError(f"{path.name} holds no ppt/slides/slideN.xml — it is an archive, but not a deck")
        out: list[dict] = []
        for number, name in slides:
            root = _parse(z, name, path)
            shape = 0
            for el in root.iter():
                if _local(el.tag) != "sp":
                    continue
                shape += 1
                text = "\n".join(t for t in (_text_of(p) for p in el.iter()
                                             if _local(p.tag) == "p") if t).strip()
                if text:
                    out.append({"locator": {"kind": "pptx", "slide": number, "shape": shape}, "text": text})
        return out


# ── xlsx ─────────────────────────────────────────────────────────────────────

def _shared_strings(z: zipfile.ZipFile, path: Path) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = _parse(z, "xl/sharedStrings.xml", path)
    return [_text_of(si) for si in root if _local(si.tag) == "si"]


def _sheet_names(z: zipfile.ZipFile, path: Path) -> list[tuple[str, str]]:
    """[(sheet name, member name)] in workbook order. A workbook without the
    relationship part falls back to the file numbering, and says so in the name
    it hands back, rather than pretending to know what the tab was called."""
    names = z.namelist()
    if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
        rels = {r.get("Id"): r.get("Target") for r in _parse(z, "xl/_rels/workbook.xml.rels", path)}
        out = []
        for sheet in _parse(z, "xl/workbook.xml", path).iter():
            if _local(sheet.tag) != "sheet":
                continue
            target = rels.get(sheet.get(_R_ID)) or ""
            member = "xl/" + target.lstrip("/")
            if member in names:
                out.append((sheet.get("name") or member, member))
        if out:
            return out
    fallback = sorted(((int(m.group(1)), m.group(0)) for m in
                       (_SHEET_RE.search(n) for n in names) if m))
    return [(f"sheet{n}", member) for n, member in fallback]


def _extract_xlsx(path: Path) -> list[dict]:
    with _open_zip(path) as z:
        sheets = _sheet_names(z, path)
        if not sheets:
            raise ExtractError(f"{path.name} holds no xl/worksheets/sheetN.xml — it is an archive, but not a workbook")
        shared = _shared_strings(z, path)
        out: list[dict] = []
        for name, member in sheets:
            root = _parse(z, member, path)
            row_number = 0
            for row in root.iter():
                if _local(row.tag) != "row":
                    continue
                row_number = int(row.get("r") or row_number + 1)
                column = 0
                for cell in row:
                    if _local(cell.tag) != "c":
                        continue
                    column += 1
                    ref = cell.get("r") or f"{_column_name(column)}{row_number}"
                    text = _cell_text(cell, shared)
                    if text:
                        out.append({"locator": {"kind": "xlsx", "sheet": name, "cell": ref}, "text": text})
        return out


def _cell_text(cell, shared: list[str]) -> str:
    """A cell that says `t="s"` holds an index into sharedStrings.xml, not a
    value; a reader that prints the index is reading a different document."""
    kind = cell.get("t")
    if kind == "inlineStr":
        return _text_of(cell).strip()
    value = next((v.text or "" for v in cell if _local(v.tag) == "v"), "")
    if kind == "s":
        try:
            return shared[int(value)].strip()
        except (ValueError, IndexError):
            return ""
    return value.strip()


def _column_name(index: int) -> str:
    name = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


# ── docx ─────────────────────────────────────────────────────────────────────

def _extract_docx(path: Path) -> list[dict]:
    with _open_zip(path) as z:
        if "word/document.xml" not in z.namelist():
            raise ExtractError(f"{path.name} holds no word/document.xml — it is an archive, but not a document")
        root = _parse(z, "word/document.xml", path)
        out: list[dict] = []
        number = 0
        for el in root.iter():
            if _local(el.tag) != "p":
                continue
            number += 1
            text = _text_of(el).strip()
            if text:
                out.append({"locator": {"kind": "docx", "paragraph": number}, "text": text})
        return out


# ── csv / markdown / text ────────────────────────────────────────────────────

def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ExtractError(f"{path.name} cannot be read: {e}") from e


def _extract_csv(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        rows = list(_csv.reader(_read_text(path).splitlines()))
    except _csv.Error as e:
        raise ExtractError(f"{path.name} is not readable as csv: {e}") from e
    for r, row in enumerate(rows, start=1):
        for c, cell in enumerate(row, start=1):
            text = (cell or "").strip()
            if text:
                out.append({"locator": {"kind": "csv", "row": r, "col": c}, "text": text})
    return out


def _extract_lines(path: Path) -> list[dict]:
    """Blank lines keep their numbers and are simply not returned. Dropping them
    from the count would renumber every line below — the fastest way to make a
    locator point at something it never named."""
    out = []
    for n, line in enumerate(_read_text(path).splitlines(), start=1):
        text = line.strip()
        if text:
            out.append({"locator": {"kind": "text", "line": n}, "text": text})
    return out


_READERS = {"pptx": _extract_pptx, "xlsx": _extract_xlsx, "docx": _extract_docx,
            "csv": _extract_csv, "text": _extract_lines}


# ── the one entry point ──────────────────────────────────────────────────────

def kind_of(path) -> str:
    """The `kind` recorded on artifact_file, or `ExtractError` naming what is known."""
    suffix = Path(path).suffix.lower()
    kind = KINDS.get(suffix)
    if kind is None:
        raise ExtractError(f"{Path(path).name}: nothing here reads {suffix or 'a file with no suffix'} — "
                           f"this module reads {', '.join(sorted({*KINDS.values()}))} "
                           f"({', '.join(sorted(KINDS))})")
    return kind


def extract(path) -> list[dict]:
    """[{locator, text}] for every piece of text in the file, in reading order.

    Raises `ExtractError` — never returns an empty list to mean "unreadable".
    """
    p = Path(path)
    kind = kind_of(p)
    if not p.is_file():
        raise ExtractError(f"{p.name} is not a file at {p}")
    return _READERS[kind](p)
