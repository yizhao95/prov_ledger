"""declared — the entry point for the world outside the code (DP phase 2c, spec §18).

One sentence from a person becomes a node in the same graph as the functions:
a business rule, an external system, a decision a group took, an external
dataset, or a number somebody computed by hand. From then on it has the same
identity, history, reasons, constraints and three views as any code node —
`provledger.declared` (the provider) projects it, and its changes are computed
like everybody else's.

**The tier is decided here, by who said what, and there is no `tier`
parameter.** A field the user typed is `stated`. A field the model tidied out
of the user's sentence is `asserted`. Confirming the row against the user's own
words makes the ROW stated without relabelling the fields the model wrote —
`field_tiers_json` keeps both answers side by side. Nothing here is ever
`observed`: nobody observed a steering-group decision.

The model's only job is tidying a sentence into `{node_type, name, attrs,
links}`. It may not name a node that is not already in the graph, and one bad
name discards the WHOLE answer (a partly-believed tidy-up is worse than none).
The runner is injected, so tests never call a model.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import provenance

NODE_TYPES = ("external_system", "business_rule", "stakeholder_decision", "external_dataset", "manual_figure")
LINK_KINDS = ("declared_feeds", "declared_constrains", "declared_depends_on")
DEFAULT_LINK_KIND = "declared_depends_on"
STATES = ("draft", "active", "retired")
PROMPT_PATH = Path(__file__).resolve().parent / "testing" / "prompts" / "declare.md"
DEFAULT_TIMEOUT_S = 120.0
QN_PREFIX = "declared:"
_JSON_RE = re.compile(r"\{.*\}", re.S)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class ModelRejected(ValueError):
    """The model's answer was discarded whole — and the caller is told why."""


# ── names ────────────────────────────────────────────────────────────────────

def slugify(text: str, *, max_words: int = 8) -> str:
    """A short, stable, lowercase name. Empty input is an error, never 'node'."""
    words = [w for w in _SLUG_RE.sub("-", (text or "").strip().lower()).split("-") if w]
    if not words:
        raise ValueError("a declared node needs a name with at least one letter or digit")
    return "-".join(words[:max_words])


def _unique_slug(conn, project: str, slug: str) -> str:
    """A new `declare` is always a NEW node (spec §18: we never judge whether
    two descriptions mean the same object), so a repeated name gets a suffix
    rather than silently joining the existing node's history."""
    taken = {r[0] for r in conn.execute("SELECT DISTINCT slug FROM declared_node WHERE project = ?", (project,))}
    if slug not in taken:
        return slug
    n = 2
    while f"{slug}-{n}" in taken:
        n += 1
    return f"{slug}-{n}"


# ── the model's tidy-up (strict, all-or-nothing) ─────────────────────────────

def prompt_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def prompt_for(description: str, known_names) -> str:
    payload = {"sentence": description, "known_names": sorted(known_names or ())}
    return prompt_text().rstrip("\n") + "\n\nMaterial:\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


def default_runner(prompt: str, *, model: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
    """Headless `claude`, no tools, no session — the same shape as the arbiter's
    runner (phase 8). Returns '' on timeout / non-zero exit / non-JSON output."""
    cmd = ["claude", "-p", "--output-format", "json", "--max-turns", "1", "--tools", "", "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout_s,
                           cwd=os.environ.get("TMPDIR") or "/tmp")
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if p.returncode != 0:
        return ""
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        return ""
    return (doc.get("result") or "") if isinstance(doc, dict) else ""


def parse_model_answer(raw: str | None, known_names) -> dict:
    """{node_type, name, attrs, links} or ModelRejected. Every rejection names
    what was wrong, because a silent fallback would look like agreement."""
    if not raw or not raw.strip():
        raise ModelRejected("the model returned nothing")
    m = _JSON_RE.search(raw)
    if not m:
        raise ModelRejected("the model's answer is not a JSON object")
    try:
        doc = json.loads(m.group(0))
    except ValueError as e:
        raise ModelRejected(f"the model's answer is not valid JSON: {e}") from None
    if not isinstance(doc, dict):
        raise ModelRejected("the model's answer is not a JSON object")
    node_type = doc.get("node_type")
    if node_type not in NODE_TYPES:
        raise ModelRejected(f"node_type {node_type!r} is not one of {list(NODE_TYPES)}")
    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ModelRejected("the model's answer has no name")
    attrs = doc.get("attrs", {})
    if not isinstance(attrs, dict):
        raise ModelRejected("attrs must be an object")
    if "tier" in attrs:
        raise ModelRejected("attrs may not carry a tier — the tier is decided by who said what")
    for k, v in attrs.items():
        if not isinstance(k, str) or not isinstance(v, (str, int, float, bool)):
            raise ModelRejected(f"attribute {k!r} must be a string, number or boolean")
    links_in = doc.get("links", [])
    if not isinstance(links_in, list):
        raise ModelRejected("links must be a list")
    links: list[tuple[str, str]] = []
    for item in links_in:
        if not isinstance(item, dict) or not isinstance(item.get("to"), str):
            raise ModelRejected("every link is an object with a string `to`")
        kind = item.get("kind", DEFAULT_LINK_KIND)
        if kind not in LINK_KINDS:
            raise ModelRejected(f"link kind {kind!r} is not one of {list(LINK_KINDS)}")
        if known_names is not None and item["to"] not in known_names:
            raise ModelRejected(f"link target {item['to']} is not a node in this project's graph — "
                                "the whole tidy-up is discarded")
        links.append((item["to"], kind))
    return {"node_type": node_type, "name": name.strip(), "attrs": attrs, "links": links}


# ── links ────────────────────────────────────────────────────────────────────

def _normalise_links(links, by: str, known_names) -> list[dict]:
    out: list[dict] = []
    for item in links or ():
        if isinstance(item, dict):
            to, kind = item.get("to"), item.get("kind", DEFAULT_LINK_KIND)
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            to, kind = item
        else:
            to, kind = item, DEFAULT_LINK_KIND
        if not isinstance(to, str) or not to:
            raise ValueError("a link needs the qualified name of an existing node")
        if kind not in LINK_KINDS:
            raise ValueError(f"link kind must be one of {LINK_KINDS}, got {kind!r}")
        if known_names is not None and to not in known_names:
            raise ValueError(f"{to} is not a node in this project's graph (nothing was written)")
        out.append({"to": to, "kind": kind, "by": by})
    out.sort(key=lambda d: (d["kind"], d["to"]))
    return out


# ── the store ────────────────────────────────────────────────────────────────

_ROW_COLUMNS = ("project", "slug", "qualified_name", "node_type", "description", "attrs_json", "links_json",
                "links_checked", "state", "tier", "field_tiers_json", "description_utterance_id", "version",
                "supersedes", "recorded_by", "model", "occurred_at", "recorded_at")


def _insert(conn, row: dict, commit: bool) -> dict:
    rid = provenance._insert_chained(conn, "declared_node", row)
    if commit:
        conn.commit()
    return get(conn, rid)


def get(conn, declared_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM declared_node WHERE id = ?", (declared_id,)).fetchone()
    return dict(r) if r else None


def active(conn, project: str) -> list[dict]:
    """The declared nodes this project has right now: the newest version of
    every slug that is active and has not been superseded."""
    rows = conn.execute("SELECT * FROM declared_node WHERE project = ? AND state = 'active' AND superseded_by IS NULL "
                        "ORDER BY slug, id", (project,)).fetchall()
    return [dict(r) for r in rows]


def history(conn, project: str, slug: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM declared_node WHERE project = ? AND slug = ? ORDER BY version, id",
                        (project, slug)).fetchall()
    return [dict(r) for r in rows]


def declare(conn, project: str, description: str, *, node_type: str | None = None, links=(), attrs=None,
            runner=None, model: str | None = None, known_names=None, occurred_at: str | None = None,
            recorded_by: str = "human", commit: bool = True) -> dict:
    """Write the DRAFT row for one sentence and return it.

    With a `runner` the model tidies the sentence into {node_type, name, attrs,
    links}; those fields are `asserted` and so is the draft row. Without one
    the user must say the type themselves (nothing is guessed) and every field
    is `stated`. There is no tier parameter: see the module docstring.
    """
    description = (description or "").strip()
    if not description:
        raise ValueError("a declared node needs the sentence that declares it")
    attrs = dict(attrs or {})
    if "tier" in attrs:
        raise ValueError("an attribute may not be called `tier` — the tier is decided by who said what")
    field_tiers: dict = {}
    user_said_type = node_type is not None
    if runner is not None:
        answer = parse_model_answer(runner(prompt_for(description, known_names), model=model), known_names)
        node_type = node_type or answer["node_type"]
        name = answer["name"]
        model_attrs = {k: v for k, v in answer["attrs"].items() if k not in attrs}
        link_rows = _normalise_links(links, "user", known_names) + _normalise_links(answer["links"], "model", known_names)
        field_tiers["node_type"] = "stated" if user_said_type else "asserted"
        field_tiers["name"] = "asserted"
        field_tiers["attrs"] = {**{k: "asserted" for k in model_attrs}, **{k: "stated" for k in attrs}}
        attrs = {**model_attrs, **attrs}
    else:
        if node_type is None:
            raise ValueError("without a model there is nothing to read the node_type out of: "
                             f"pass --type ({' | '.join(NODE_TYPES)})")
        name = description
        link_rows = _normalise_links(links, "user", known_names)
        field_tiers["node_type"] = "stated"
        field_tiers["name"] = "stated"
        field_tiers["attrs"] = {k: "stated" for k in attrs}
    if node_type not in NODE_TYPES:
        raise ValueError(f"node_type must be one of {NODE_TYPES}, got {node_type!r}")
    link_rows.sort(key=lambda d: (d["kind"], d["to"]))
    field_tiers["links"] = ["stated" if d["by"] == "user" else "asserted" for d in link_rows]
    slug = _unique_slug(conn, project, slugify(name))
    row = {"project": project, "slug": slug, "qualified_name": QN_PREFIX + slug, "node_type": node_type,
           "description": description, "attrs_json": json.dumps(attrs, sort_keys=True, ensure_ascii=False),
           "links_json": json.dumps(link_rows, sort_keys=True, ensure_ascii=False),
           "links_checked": 1 if known_names is not None else 0, "state": "draft",
           "tier": "asserted" if runner is not None else "stated",
           "field_tiers_json": json.dumps(field_tiers, sort_keys=True, ensure_ascii=False),
           "description_utterance_id": None, "version": 1, "supersedes": None,
           "recorded_by": "agent" if runner is not None else recorded_by, "model": model,
           "occurred_at": occurred_at or provenance._db_now(conn), "recorded_at": provenance._db_now(conn)}
    return _insert(conn, row, commit)


def _next_version(conn, previous: dict, changes: dict, commit: bool) -> dict:
    row = {k: previous[k] for k in _ROW_COLUMNS}
    row.update(changes)
    row["version"] = int(previous["version"]) + 1
    row["supersedes"] = previous["id"]
    row["recorded_at"] = provenance._db_now(conn)
    new = _insert(conn, row, commit=False)
    conn.execute("UPDATE declared_node SET superseded_by = ? WHERE id = ?", (new["id"], previous["id"]))
    if commit:
        conn.commit()
    return get(conn, new["id"])


def confirm(conn, draft_id: int, utterance_id: int, *, commit: bool = True) -> dict:
    """The user says the words that put this node in the graph. The row becomes
    `stated` and active; the fields the model wrote keep saying so."""
    draft = get(conn, draft_id)
    if draft is None:
        raise ValueError(f"declared node {draft_id} does not exist")
    if draft["superseded_by"] is not None:
        raise ValueError(f"declared node {draft_id} was already superseded by {draft['superseded_by']}")
    if provenance.get_utterance(conn, int(utterance_id)) is None:
        raise ValueError(f"utterance {utterance_id} does not exist — a confirmation needs the words it confirms")
    return _next_version(conn, draft, {"state": "active", "tier": "stated",
                                       "description_utterance_id": int(utterance_id),
                                       "recorded_by": "human"}, commit)


def revise(conn, declared_id: int, *, description: str | None = None, node_type: str | None = None,
           attrs=None, links=None, known_names=None, utterance_id: int | None = None,
           commit: bool = True) -> dict:
    """A change to a declared node is a new version row, never an edit. The
    fields the user changes here are theirs, so they become `stated`."""
    previous = get(conn, declared_id)
    if previous is None:
        raise ValueError(f"declared node {declared_id} does not exist")
    if previous["superseded_by"] is not None:
        raise ValueError(f"declared node {declared_id} was already superseded by {previous['superseded_by']}")
    field_tiers = json.loads(previous["field_tiers_json"] or "{}")
    changes: dict = {"state": "active"}
    if description is not None:
        changes["description"] = description.strip()
    if node_type is not None:
        if node_type not in NODE_TYPES:
            raise ValueError(f"node_type must be one of {NODE_TYPES}, got {node_type!r}")
        changes["node_type"] = node_type
        field_tiers["node_type"] = "stated"
    if attrs is not None:
        attrs = dict(attrs)
        if "tier" in attrs:
            raise ValueError("an attribute may not be called `tier`")
        changes["attrs_json"] = json.dumps(attrs, sort_keys=True, ensure_ascii=False)
        field_tiers["attrs"] = {k: "stated" for k in attrs}
    if links is not None:
        rows = _normalise_links(links, "user", known_names)
        changes["links_json"] = json.dumps(rows, sort_keys=True, ensure_ascii=False)
        changes["links_checked"] = 1 if known_names is not None else 0
        field_tiers["links"] = ["stated" for _ in rows]
    if utterance_id is not None:
        if provenance.get_utterance(conn, int(utterance_id)) is None:
            raise ValueError(f"utterance {utterance_id} does not exist")
        changes["description_utterance_id"] = int(utterance_id)
        changes["tier"] = "stated"
        changes["recorded_by"] = "human"
    changes["field_tiers_json"] = json.dumps(field_tiers, sort_keys=True, ensure_ascii=False)
    return _next_version(conn, previous, changes, commit)


def retire(conn, declared_id: int, *, utterance_id: int | None = None, commit: bool = True) -> dict:
    """Retiring appends a `retired` row: the node leaves the active set and the
    next analysis run computes its removal like any other node's."""
    previous = get(conn, declared_id)
    if previous is None:
        raise ValueError(f"declared node {declared_id} does not exist")
    if previous["superseded_by"] is not None:
        raise ValueError(f"declared node {declared_id} was already superseded by {previous['superseded_by']}")
    changes: dict = {"state": "retired"}
    if utterance_id is not None:
        if provenance.get_utterance(conn, int(utterance_id)) is None:
            raise ValueError(f"utterance {utterance_id} does not exist")
        changes["description_utterance_id"] = int(utterance_id)
        changes["tier"] = "stated"
        changes["recorded_by"] = "human"
    return _next_version(conn, previous, changes, commit)
