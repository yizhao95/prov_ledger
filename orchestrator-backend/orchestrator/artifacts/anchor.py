"""artifacts.anchor — a person pins one place in a file to one node.

DP phase 4, spec §9 (D9 / D10). Three rules, and the third is a veto item.

**A person does the pinning.** `anchor()` is only ever reached from
`provledger anchor <file> --at "slide 4" --node metric:q3_conv --value 3.2`.
The node must already exist — a figure's identity is its data source, so an
anchor cannot invent one — and the value must really be at that place, or the
call is refused with the first 80 characters of what IS there.

**A locator that breaks says so.** `check()` re-reads the file and looks at the
stored locator, and only at it. File gone, locator gone, value gone: each is
`anchor_lost` with the reason in words, appended to `anchor_state`.

**A locator is never re-pointed.** This is F4, and it is a veto item. When the
number turns up two slides later, `check` still reports `anchor_lost`; it does
not follow. `occurrence` refuses UPDATE in SQL so that no future caller can do
it either. Citing the wrong slide is worse than admitting the pointer broke —
a ledger that quietly re-aims is a ledger nobody can audit.

**Auto-discovery is off** (F5). `candidates()` refuses to run unless somebody
switched it on, and even then it only lists suggestions: it writes no
occurrence, ever. Pointing at the wrong number is worse than pointing at none.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .. import provenance
from . import extract

METRIC_PREFIX = "metric:"
DECLARED_PREFIX = "declared:"

_AT_FORMS = (
    'slide <n> [shape <n>]        a deck        e.g. "slide 4", "slide 4 shape 2"',
    '<sheet>!<cell> | <cell>      a workbook    e.g. "Q3!B7", "B7"',
    'paragraph <n>                a report      e.g. "paragraph 3"',
    'row <n> [col <n>]            a csv         e.g. "row 2 col 2"',
    'line <n>                     text or md    e.g. "line 3"',
)
_CELL_ONLY = re.compile(r"^([A-Za-z]{1,3})([0-9]{1,7})$")
_SHEET_CELL = re.compile(r"^(?P<sheet>.+)!(?P<cell>[A-Za-z]{1,3}[0-9]{1,7})$")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


class AnchorError(ValueError):
    """An anchor that cannot be made, or a request that is switched off — with
    the reason in words. Nothing here ever fails quietly."""


# ── the `--at` grammar ───────────────────────────────────────────────────────

def parse_at(spec: str) -> dict:
    """The words a person types into `--at`, as a partial locator.

    Partial on purpose: "slide 4" names a slide, and any shape on it will do.
    `describe()` turns a locator back into the same words.
    """
    text = (spec or "").strip()
    if not text:
        raise AnchorError("--at needs a place in the file. " + _forms())
    m = _SHEET_CELL.match(text)
    if m:
        return {"kind": "xlsx", "sheet": m.group("sheet").strip(), "cell": m.group("cell").upper()}
    m = _CELL_ONLY.match(text)
    if m and not text.lower().startswith(("row", "col", "line")):
        return {"kind": "xlsx", "cell": (m.group(1) + m.group(2)).upper()}
    words = text.replace(",", " ").split()
    pairs: dict[str, int] = {}
    i = 0
    while i + 1 < len(words):
        key, value = words[i].lower().rstrip(":"), words[i + 1]
        if key in ("slide", "shape", "paragraph", "para", "row", "col", "column", "line") and value.isdigit():
            pairs[{"para": "paragraph", "column": "col"}.get(key, key)] = int(value)
            i += 2
        else:
            i += 1
    if "slide" in pairs:
        return {"kind": "pptx", **{k: v for k, v in pairs.items() if k in ("slide", "shape")}}
    if "paragraph" in pairs:
        return {"kind": "docx", "paragraph": pairs["paragraph"]}
    if "row" in pairs:
        return {"kind": "csv", **{k: v for k, v in pairs.items() if k in ("row", "col")}}
    if "line" in pairs:
        return {"kind": "text", "line": pairs["line"]}
    raise AnchorError(f"--at {spec!r} is not a place this tool knows. " + _forms())


def _forms() -> str:
    return "Say it one of these ways:\n  " + "\n  ".join(_AT_FORMS)


def describe(locator: dict) -> str:
    """A locator in the words a person would use for it."""
    kind = locator.get("kind")
    if kind == "pptx":
        return f"slide {locator['slide']}" + (f" shape {locator['shape']}" if locator.get("shape") else "")
    if kind == "xlsx":
        sheet = locator.get("sheet")
        return f"{sheet}!{locator['cell']}" if sheet else str(locator.get("cell"))
    if kind == "docx":
        return f"paragraph {locator['paragraph']}"
    if kind == "csv":
        return f"row {locator['row']}" + (f" col {locator['col']}" if locator.get("col") else "")
    if kind == "text":
        return f"line {locator['line']}"
    return json.dumps(locator, sort_keys=True)


def _matches(locator: dict, at: dict) -> bool:
    """Every key the person named must agree. Keys they left out are free."""
    return all(locator.get(k) == v for k, v in at.items())


# ── does this value really sit there ─────────────────────────────────────────

def holds_value(text: str, value: str) -> bool:
    """Whether this piece of text carries this value.

    A number needs a boundary: `3.2` is not in `13.28`, and an anchor that
    thought it was would report `ok` for ever while pointing at somebody else's
    figure. Non-numeric values fall back to plain containment.
    """
    value = (value or "").strip()
    if not value:
        return False
    if _NUMBER.fullmatch(value):
        return re.search(r"(?<![\d.])" + re.escape(value) + r"(?![\d])", text) is not None
    return value in text


def _as_number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def preview(text: str, limit: int = 80) -> str:
    """The first `limit` characters of what is actually at a place — the tool
    says what it found, so the person can see why the anchor was refused."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# ── what a node has to be ────────────────────────────────────────────────────

def node_exists(conn, project: str, node_key: str, *, known_nodes=None, psg_db_path=None) -> bool:
    """A figure's identity is its data source, so an anchor may only point at
    one that is already there: a metric this project has recorded, a column in
    its state graph, or something somebody declared (026)."""
    if node_key.startswith(METRIC_PREFIX):
        name = node_key[len(METRIC_PREFIX):]
        return bool(conn.execute("SELECT 1 FROM metrics WHERE project = ? AND name = ? LIMIT 1",
                                 (project, name)).fetchone())
    if node_key.startswith(DECLARED_PREFIX):
        return bool(conn.execute("SELECT 1 FROM declared_node WHERE project = ? AND qualified_name = ? "
                                 "AND state = 'active' AND superseded_by IS NULL LIMIT 1",
                                 (project, node_key)).fetchone())
    if known_nodes is not None:
        return node_key in known_nodes
    from .. import psg_bridge
    path = psg_db_path or psg_bridge.db_path_for(project)
    if not path or not os.path.exists(path):
        return False
    return bool(psg_bridge.node_key_of(path, node_key))


def _refuse_unknown_node(project: str, node_key: str) -> AnchorError:
    return AnchorError(
        f"{node_key} is not a node of {project}. A figure's identity is its data source, so an anchor "
        f"points at one that already exists:\n"
        f"  metric:<name>        a metric this project recorded (provledger metrics / record-metric)\n"
        f"  <dataset>.<column>   a column in this project's state graph\n"
        f"  declared:<slug>      something declared with `provledger node declare` or `node add --manual-figure`")


# ── files ────────────────────────────────────────────────────────────────────

def sha256_of(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def register_file(conn, project: str, path: str, sha256: str, kind: str, *, commit: bool = True) -> int:
    """The artifact_file row for these exact bytes at this exact path. A new
    version of the deck is a NEW row: the sha256 is part of what the row is."""
    row = conn.execute("SELECT id FROM artifact_file WHERE project = ? AND path = ? AND sha256 = ?",
                       (project, path, sha256)).fetchone()
    if row:
        conn.execute("UPDATE artifact_file SET last_seen = strftime('%Y-%m-%d %H:%M:%S', 'now') WHERE id = ?",
                     (row[0],))
        file_id = int(row[0])
    else:
        cur = conn.execute("INSERT INTO artifact_file (project, path, sha256, kind) VALUES (?, ?, ?, ?)",
                           (project, path, sha256, kind))
        file_id = int(cur.lastrowid)
    if commit:
        conn.commit()
    return file_id


def get_file(conn, file_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM artifact_file WHERE id = ?", (file_id,)).fetchone()
    return dict(r) if r else None


def get(conn, occurrence_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM occurrence WHERE id = ?", (occurrence_id,)).fetchone()
    return dict(r) if r else None


def for_node(conn, project: str, node_key: str) -> list[dict]:
    """Every occurrence of one node, newest last, each with its file and its
    latest check — what the node page's Occurrences section prints."""
    rows = conn.execute(
        "SELECT o.*, f.path AS file_path, f.kind AS file_kind, f.sha256 AS file_sha256 "
        "FROM occurrence o JOIN artifact_file f ON f.id = o.file_id "
        "WHERE o.project = ? AND o.node_key = ? ORDER BY o.id", (project, node_key)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["locator"] = json.loads(d["locator_json"] or "{}")
        d["where"] = describe(d["locator"])
        d["state"] = latest_state(conn, d["id"])
        out.append(d)
    return out


def latest_state(conn, occurrence_id: int) -> dict | None:
    r = conn.execute("SELECT state, checked_at, reason FROM anchor_state WHERE occurrence_id = ? "
                     "ORDER BY id DESC LIMIT 1", (occurrence_id,)).fetchone()
    return dict(r) if r else None


# ── anchoring ────────────────────────────────────────────────────────────────

def anchor(conn, project: str, file, at: str, node: str, value: str, *, by: str = "human",
           seen_at: str | None = None, stored_path: str | None = None, known_nodes=None,
           psg_db_path=None, commit: bool = True) -> dict:
    """Record that `value` is at `at` in `file`, and that it is a reading of `node`.

    Refuses — loudly, with the reason — when the node does not exist, when the
    place does not exist, or when the value is not there.
    """
    path = Path(file)
    if not node_exists(conn, project, node, known_nodes=known_nodes, psg_db_path=psg_db_path):
        raise _refuse_unknown_node(project, node)
    entries = extract.extract(path)                       # ExtractError propagates: an unreadable file is not an anchor
    want = parse_at(at)
    here = [e for e in entries if _matches(e["locator"], want)]
    if not here:
        places = ", ".join(sorted({describe(e["locator"]) for e in entries})[:6])
        raise AnchorError(f"{at!r} is not a place in {path.name}. It holds: {places}"
                          + (" …" if len(entries) > 6 else ""))
    hit = next((e for e in here if holds_value(e["text"], value)), None)
    if hit is None:
        raise AnchorError(f"{value} is not at {at!r} in {path.name}. What is there: "
                          + " / ".join(preview(e["text"]) for e in here[:3]))
    kind = extract.kind_of(path)
    recorded_path = stored_path or str(file)
    file_id = register_file(conn, project, recorded_path, sha256_of(path), kind, commit=False)
    locator = dict(hit["locator"], at=at.strip())
    row = {"project": project, "node_key": node, "file_id": file_id,
           "locator_json": json.dumps(locator, sort_keys=True, ensure_ascii=False),
           "value_text": str(value).strip(), "value_num": _as_number(value),
           "seen_at": seen_at or provenance._db_now(conn), "tier": "observed", "by": by,
           "recorded_at": provenance._db_now(conn)}
    rid = provenance._insert_chained(conn, "occurrence", row)
    if commit:
        conn.commit()
    out = get(conn, rid)
    out["locator"] = locator
    out["where"] = describe(locator)
    out["file"] = get_file(conn, file_id)
    return out


# ── checking ─────────────────────────────────────────────────────────────────

def _verdict(occurrence: dict, root: str | None) -> tuple[str, str | None, str | None]:
    """(state, reason, what_is_there_now) — the whole of the F4 decision.

    It looks at the place the person named and at nothing else. There is
    deliberately no branch in this function that searches the rest of the file:
    the number being somewhere else is exactly the case `anchor_lost` exists to
    report.

    The place it looks at is the one they TYPED — `--at "slide 4"` means the
    slide, so a deck that gains a text box above the figure does not lose the
    anchor, while somebody who typed `slide 4 shape 2` asked a narrower
    question and gets the narrower answer. The resolved locator stays on the
    row as the record of where the number was when they looked.
    """
    locator = json.loads(occurrence["locator_json"] or "{}")
    at = locator.get("at") or describe(locator)
    path = Path(occurrence["file_path"])
    if not path.is_absolute() and root:
        path = Path(root) / path
    if not path.is_file():
        return "anchor_lost", f"the file is no longer at {occurrence['file_path']}", None
    try:
        entries = extract.extract(path)
    except extract.ExtractError as e:
        return "anchor_lost", f"the file can no longer be read: {e}", None
    try:
        target = parse_at(locator["at"]) if locator.get("at") else {k: v for k, v in locator.items() if k != "at"}
    except AnchorError:
        target = {k: v for k, v in locator.items() if k != "at"}
    here = [e for e in entries if _matches(e["locator"], target)]
    if not here:
        return "anchor_lost", f"{at} is not in this file any more", None
    value = occurrence["value_text"]
    if any(holds_value(e["text"], value) for e in here):
        return "ok", None, None
    now = " / ".join(preview(e["text"]) for e in here[:3])
    return "anchor_lost", f"{value} is no longer at {at}: moved or removed", now


def check(conn, occurrence_id: int, *, root: str | None = None, commit: bool = True) -> dict:
    """Look at one anchor's place again and append what was found.

    Never re-points. When the value turns up elsewhere in the file this still
    returns `anchor_lost`; where it went is a question this function refuses to
    answer, because answering it wrongly is how a ledger starts citing the
    wrong slide.
    """
    row = conn.execute("SELECT o.*, f.path AS file_path, f.kind AS file_kind FROM occurrence o "
                       "JOIN artifact_file f ON f.id = o.file_id WHERE o.id = ?", (occurrence_id,)).fetchone()
    if row is None:
        raise AnchorError(f"occurrence {occurrence_id} does not exist")
    occurrence = dict(row)
    state, reason, now = _verdict(occurrence, root)
    conn.execute("INSERT INTO anchor_state (occurrence_id, state, reason) VALUES (?, ?, ?)",
                 (occurrence_id, state, reason))
    if commit:
        conn.commit()
    locator = json.loads(occurrence["locator_json"] or "{}")
    return {"occurrence_id": occurrence_id, "node_key": occurrence["node_key"], "state": state,
            "reason": reason, "found_instead": now, "value": occurrence["value_text"],
            "file": occurrence["file_path"], "where": locator.get("at") or describe(locator),
            "locator": locator}


def check_project(conn, project: str, *, root: str | None = None, commit: bool = True) -> list[dict]:
    """Every anchor this project holds, checked once, oldest first."""
    ids = [r[0] for r in conn.execute("SELECT id FROM occurrence WHERE project = ? ORDER BY id", (project,))]
    out = [check(conn, i, root=root, commit=False) for i in ids]
    if commit:
        conn.commit()
    return out


def lost_count(conn, project: str | None = None) -> int:
    """How many anchors were `anchor_lost` the last time anybody looked."""
    sql = ("SELECT COUNT(*) FROM occurrence o WHERE (SELECT s.state FROM anchor_state s "
           "WHERE s.occurrence_id = o.id ORDER BY s.id DESC LIMIT 1) = 'anchor_lost'")
    params: tuple = ()
    if project:
        sql += " AND o.project = ?"
        params = (project,)
    return int(conn.execute(sql, params).fetchone()[0])


# ── auto-discovery, which is off ─────────────────────────────────────────────

OFF_MESSAGE = (
    "auto-discovery is off by default (spec §9, F5). It proposes candidates only — it never writes an "
    "occurrence, because pointing at the wrong number is worse than pointing at none. Switch it on with "
    '`{"reasons": {"auto_discover": true}}` in provledger-extensions.json, or pass '
    "--i-know-this-is-off-by-default.")


def candidates(conn, project: str, file, *, enabled: bool = False) -> list[dict]:
    """Places in this file whose number equals a metric this project recorded.

    Suggestions, tier `asserted`, written nowhere. A person still has to run
    `provledger anchor`, because a person is what the tier `observed` means.
    """
    if not enabled:
        raise AnchorError(OFF_MESSAGE)
    known = {}
    for name, value in conn.execute("SELECT name, value FROM metrics WHERE project = ?", (project,)):
        known.setdefault(_fmt(value), set()).add(METRIC_PREFIX + name)
    out: list[dict] = []
    for entry in extract.extract(Path(file)):
        for number in _NUMBER.findall(entry["text"]):
            for node_key in sorted(known.get(_fmt(number), ())):
                out.append({"locator": entry["locator"], "where": describe(entry["locator"]),
                            "value": number, "node_key": node_key, "tier": "asserted",
                            "preview": preview(entry["text"]),
                            "anchor_with": f'provledger anchor <file> --at "{describe(entry["locator"])}" '
                                           f'--node {node_key} --value {number}'})
    return out


def _fmt(value) -> str:
    """One spelling for a number, so 3.2 and 3.20 meet."""
    n = _as_number(value) if not isinstance(value, (int, float)) else float(value)
    if n is None:
        return str(value)
    return f"{n:.10g}"
