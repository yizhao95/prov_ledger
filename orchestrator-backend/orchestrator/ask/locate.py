"""ask.locate — which nodes a question is about (DP phase 2e, Task 0; spec §21).

Two steps, and the split between them is the point:

  `candidates()` is CODE. Three literal matchers over the ledger and the graph —
  full text (FTS5 from migration 019, LIKE where the build has none) over
  reasons, the words they quote and the labels of the sources they link; the
  node names and file names the graph knows; and the identifiers and numbers
  written in the question (``train_test_split``, ``80/20``). Every candidate
  carries a `why` saying which matcher found it. The list is capped at
  MAX_CANDIDATES and the cap is reported, never silent.

  `choose()` is the MODEL, and it may only pick FROM that list. The answer must
  be strict JSON ``{"chosen": [qualified names], "basis": "…"}`` with at most
  MAX_CHOSEN names. Anything else — non-JSON, a name nobody proposed, too many
  names, no runner at all — falls back to the top FALLBACK_TOP by match score
  and says so in `basis`. The choice is asserted; it is written to `ask_log`.
"""
from __future__ import annotations

import json
import re
import sqlite3

from .. import why as why_mod

MAX_CANDIDATES = 40
MAX_CHOSEN = 8
FALLBACK_TOP = 5
TEXT_LIMIT = 60                 # ledger rows one matcher may pull before scoring

# scores: a name the user wrote outranks a word that happens to appear somewhere
SCORE_NAME = 6
SCORE_LITERAL = 5
SCORE_FILE = 3
SCORE_TEXT = 2

_JSON_RE = re.compile(r"\{.*\}", re.S)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:_[A-Za-z0-9_]+)+|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_NUMERIC = re.compile(r"\d+(?:\.\d+)?\s*[/:]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?%|\d+(?:\.\d+)?")
_FILE = re.compile(r"[\w./-]+\.[A-Za-z0-9]{1,5}")
STOPWORDS = {
    "the", "and", "why", "does", "did", "was", "were", "are", "for", "from", "with", "that", "this", "have", "has",
    "had", "not", "but", "you", "our", "its", "who", "how", "what", "when", "where", "which", "there", "here",
    "any", "all", "can", "could", "would", "should", "into", "onto", "over", "under", "about", "ever", "tried",
    "try", "trying", "test", "tests", "tested", "come", "comes", "came", "get", "got", "use", "used", "using",
    "make", "made", "still", "yet", "ask", "asked", "one", "two", "some", "much", "many", "more", "most", "why?",
}


# ── what the question literally says ─────────────────────────────────────────

def question_tokens(question: str) -> list[str]:
    """Content words (≥ 3 letters, lowercased, stopwords out), in order, unique."""
    out: list[str] = []
    for w in _WORD.findall(question or ""):
        low = w.lower()
        if low in STOPWORDS or low in out:
            continue
        out.append(low)
    return out


def question_literals(question: str) -> list[str]:
    """Identifiers and numbers exactly as written: `train_test_split`, `pkg.m.f`, `80/20`, `0.2`, `95%`."""
    out: list[str] = []
    for m in list(_IDENT.finditer(question or "")) + list(_NUMERIC.finditer(question or "")):
        lit = m.group(0).strip()
        if lit and lit not in out:
            out.append(lit)
    return out


def question_files(question: str) -> list[str]:
    return [m.group(0) for m in _FILE.finditer(question or "")]


# ── the matchers (all code, all literal) ─────────────────────────────────────

def _text_hits(conn, project: str, tokens: list[str], limit: int) -> list[dict]:
    """change_reason rows matching any token — FTS5 (019) first, LIKE per token otherwise."""
    if not tokens:
        return []
    if why_mod.ensure_fts(conn):
        try:
            return why_mod._fts_query(conn, project, " OR ".join(f'"{t}"' for t in tokens), limit)
        except sqlite3.OperationalError:
            pass
    rows: list[dict] = []
    seen: set[int] = set()
    for t in tokens:
        for r in why_mod._like_query(conn, project, t, limit):
            if r["id"] not in seen:
                seen.add(r["id"])
                rows.append(r)
    return rows[:limit]


def _utterance_hits(conn, project: str, tokens: list[str], limit: int) -> list[tuple[int, str, int]]:
    """(reason_id, node_key, utterance_id) where the quoted words contain a token."""
    if not tokens:
        return []
    where = " OR ".join("u.text LIKE ?" for _ in tokens)
    params = [project] + [f"%{t}%" for t in tokens] + [limit]
    return [(r[0], r[1], r[2]) for r in conn.execute(
        f"SELECT r.id, r.node_key, u.id FROM change_reason r JOIN utterance u ON u.id = r.verbatim_utterance_id "
        f"WHERE r.project = ? AND r.node_key IS NOT NULL AND ({where}) ORDER BY r.id DESC LIMIT ?", params)]


def _reference_hits(conn, project: str, tokens: list[str], limit: int) -> list[tuple[int, str, int, str]]:
    """(reason_id, node_key, reference_id, label) where a linked source's label contains a token."""
    if not tokens:
        return []
    where = " OR ".join("f.label LIKE ?" for _ in tokens)
    params = [project] + [f"%{t}%" for t in tokens] + [limit]
    return [(r[0], r[1], r[2], r[3]) for r in conn.execute(
        f"SELECT r.id, r.node_key, f.id, f.label FROM reference_link l JOIN reference f ON f.id = l.reference_id "
        f"JOIN change_reason r ON r.id = l.reason_id "
        f"WHERE r.project = ? AND r.node_key IS NOT NULL AND ({where}) ORDER BY r.id DESC LIMIT ?", params)]


# `node_snapshot` holds one row per node PER RUN — on this repo, ~2,900 nodes over
# ~100 runs. The first version of this matcher asked for every node's latest row
# with a correlated subquery (`run_id = (SELECT MAX(run_id) … WHERE x.node_key =
# node_snapshot.node_key)`) and then compared names in Python. There is no index
# on `node_key` alone, so the subquery re-scanned the table once per row: the
# first real page load spent **183 of its 185 seconds inside that one query**.
#
# Two changes, both "ask the database the question you actually have":
#   · the name / file predicates go INTO the query, so only candidate rows come
#     back instead of the whole graph;
#   · the latest run is a GROUP BY with a bare MAX (SQLite takes the other
#     columns from the max row), so the table is scanned once, not once per row.

def _name_predicates(tokens: list[str], literals: list[str], files: list[str]) -> tuple[str, list]:
    """SQL that matches a node by its own name, its dotted path, or its file."""
    where: list[str] = []
    params: list = []
    for t in {*tokens, *(x.lower() for x in literals)}:
        if not t:
            continue
        where.append("lower(qualified_name) = ?")
        params.append(t)
        where.append("lower(qualified_name) LIKE ?")
        params.append(f"%.{t}")
    for lit in literals:                       # a dotted or underscored literal may sit mid-path
        low = lit.lower()
        if len(low) > 3:
            where.append("lower(qualified_name) LIKE ?")
            params.append(f"%{low}%")
    for f in files:
        base = f.rsplit("/", 1)[-1].lower()
        if base:
            where.append("lower(file_path) LIKE ?")
            params.append(f"%/{base}")
            where.append("lower(file_path) = ?")
            params.append(base)
    return (" OR ".join(where), params)


def _graph_matches(psg_db_path: str | None, tokens: list[str], literals: list[str],
                   files: list[str], limit: int) -> list[tuple[str, str, str]]:
    """(node_key, qualified_name, file_path) for nodes the question NAMES, each
    at its latest run. Never the whole graph."""
    where, params = _name_predicates(tokens, literals, files)
    if not where:
        return []
    rows = why_mod.psg_bridge._query(
        psg_db_path,
        f"SELECT node_key, qualified_name, file_path, MAX(run_id) FROM node_snapshot "
        f"WHERE node_key <> '' AND ({where}) GROUP BY node_key LIMIT ?", (*params, limit))
    return [(r[0], r[1], r[2]) for r in rows]


def _names_of(psg_db_path: str | None, node_keys: list[str]) -> dict[str, str]:
    """The latest qualified name of the keys the TEXT matchers found — one query
    for all of them, not one per key."""
    keys = [k for k in dict.fromkeys(node_keys) if k]
    if not keys:
        return {}
    ph = ",".join("?" * len(keys))
    rows = why_mod.psg_bridge._query(
        psg_db_path,
        f"SELECT node_key, qualified_name, MAX(run_id) FROM node_snapshot "
        f"WHERE node_key IN ({ph}) GROUP BY node_key", tuple(keys))
    return {r[0]: r[1] for r in rows}


def _literal_text_hits(conn, project: str, literals: list[str], limit: int) -> list[tuple[str, int, str]]:
    """(literal, reason_id, node_key) where the literal appears verbatim in a statement or interpretation."""
    out: list[tuple[str, int, str]] = []
    for lit in literals:
        like = f"%{lit}%"
        for r in conn.execute(
                "SELECT id, node_key FROM change_reason WHERE project = ? AND node_key IS NOT NULL "
                "AND (statement LIKE ? OR interpretation LIKE ?) ORDER BY id DESC LIMIT ?", (project, like, like, limit)):
            out.append((lit, r[0], r[1]))
    return out


def _latest_text(conn, project: str, node_keys: list[str]) -> dict[str, str]:
    """The most recent record's text per node — the one line the prompt shows per candidate."""
    if not node_keys:
        return {}
    ph = ",".join("?" * len(node_keys))
    out: dict[str, str] = {}
    for key, text in conn.execute(
            f"SELECT node_key, COALESCE(interpretation, statement) FROM change_reason "
            f"WHERE project = ? AND node_key IN ({ph}) ORDER BY id ASC", (project, *node_keys)):
        if text:
            out[key] = text.replace("\n", " ")[:160]
    return out


# ── candidates ───────────────────────────────────────────────────────────────

def candidates(conn, psg_db_path: str | None, question: str, *, project: str, limit: int = MAX_CANDIDATES) -> list[dict]:
    """Every node the question literally touches, with the matcher that found it.

    Returns `[{node_key, qn, why, score, records, latest}]`, best match first,
    at most `limit` entries (the caller reports the cap in the scope line)."""
    tokens = question_tokens(question)
    literals = question_literals(question)
    files = question_files(question)
    pool: dict[str, dict] = {}

    def add(node_key: str | None, qn: str | None, why: str, score: int, record_id: int | None = None) -> None:
        key = node_key or qn
        if not key:
            return
        e = pool.setdefault(key, {"node_key": node_key, "qn": qn or key, "why": [], "score": 0, "records": []})
        if node_key and not e["node_key"]:
            e["node_key"] = node_key
        if qn and (e["qn"] == key or not e["qn"]):
            e["qn"] = qn
        if why not in e["why"]:
            e["why"].append(why)
        e["score"] += score
        if record_id is not None and record_id not in e["records"]:
            e["records"].append(record_id)

    # 1 · full text over the ledger: the reasons themselves, the words they quote, the sources they link
    for row in _text_hits(conn, project, tokens, TEXT_LIMIT):
        if row.get("node_key"):
            add(row["node_key"], None, f"text match: reason #{row['id']}", SCORE_TEXT, row["id"])
    for reason_id, node_key, utt_id in _utterance_hits(conn, project, tokens, TEXT_LIMIT):
        add(node_key, None, f"text match: utterance #{utt_id} quoted by #{reason_id}", SCORE_TEXT, reason_id)
    for reason_id, node_key, ref_id, label in _reference_hits(conn, project, tokens, TEXT_LIMIT):
        add(node_key, None, f"text match: source #{ref_id} ({label[:60]}) linked from #{reason_id}", SCORE_TEXT, reason_id)

    # 2 · the names the graph knows — the node's own name, its dotted path, its file
    graph_nodes = _graph_matches(psg_db_path, tokens, literals, files, max(limit, MAX_CANDIDATES) * 4)
    by_key = {k: qn for k, qn, _ in graph_nodes}
    for node_key, qn, file_path in graph_nodes:
        low = (qn or "").lower()
        short = low.rsplit(".", 1)[-1]
        for t in tokens:
            if t == short or t == low:
                add(node_key, qn, f"name match: {qn.rsplit('.', 1)[-1]}", SCORE_NAME)
        for lit in literals:
            ll = lit.lower()
            if ll and (ll == short or ll == low or (len(ll) > 3 and ll in low)):
                add(node_key, qn, f"name match: {lit}", SCORE_NAME)
        base = (file_path or "").rsplit("/", 1)[-1].lower()
        for f in files:
            if base and base == f.rsplit("/", 1)[-1].lower():
                add(node_key, qn, f"file match: {base}", SCORE_FILE)

    # 3 · the identifiers and numbers written in the question, matched verbatim in the ledger
    for lit, reason_id, node_key in _literal_text_hits(conn, project, literals, TEXT_LIMIT):
        add(node_key, None, f"literal match: {lit} in reason #{reason_id}", SCORE_LITERAL, reason_id)

    unnamed = [e["node_key"] for e in pool.values()
               if e["node_key"] and (not e["qn"] or e["qn"] == e["node_key"]) and e["node_key"] not in by_key]
    by_key.update(_names_of(psg_db_path, unnamed))
    for e in pool.values():
        if not e["qn"] or e["qn"] == e["node_key"]:
            e["qn"] = by_key.get(e["node_key"]) or e["node_key"]
    latest = _latest_text(conn, project, [e["node_key"] for e in pool.values() if e["node_key"]])
    out = []
    for e in sorted(pool.values(), key=lambda x: (-x["score"], x["qn"]))[:limit]:
        out.append({"node_key": e["node_key"], "qn": e["qn"], "why": "; ".join(e["why"]),
                    "score": e["score"], "records": e["records"], "latest": latest.get(e["node_key"] or "", "")})
    return out


# ── choose: the model, inside the fence ──────────────────────────────────────

PROMPT = """You are picking which nodes a question is about. You do not answer the question.

Rules:
- Choose ONLY from the candidate list below. A name that is not in the list is an error.
- Choose at most {max_chosen} names, fewest first: the ones the question is actually about.
- Reply with ONE strict JSON object and nothing else:
  {{"chosen": ["<qualified name>", ...], "basis": "<one short sentence>"}}

Question:
{question}

Candidates:
{candidates}
"""


def prompt_for(question: str, cands: list[dict], max_chosen: int = MAX_CHOSEN) -> str:
    lines = []
    for c in cands:
        line = f"- {c['qn']} · {c.get('why', '')}"
        if c.get("latest"):
            line += f" · latest record: {c['latest']}"
        lines.append(line)
    return PROMPT.format(max_chosen=max_chosen, question=question.strip(), candidates="\n".join(lines) or "(none)")


def _fallback(cands: list[dict], basis: str, rejected: str | None = None) -> dict:
    return {"chosen": [c["qn"] for c in cands[:FALLBACK_TOP]], "basis": basis, "fallback": True,
            "rejected": rejected, "raw": None}


def choose(question: str, cands: list[dict], *, runner=None, model: str | None = None, timeout_s: float | None = None,
           max_chosen: int = MAX_CHOSEN) -> dict:
    """The model picks among `cands`; anything off the list degrades to the top matches.

    Returns `{chosen: [qualified name], basis, fallback, rejected, raw}`."""
    if runner is None:
        return _fallback(cands, "fallback: no model")
    kwargs = {"model": model}
    if timeout_s is not None:
        kwargs["timeout_s"] = timeout_s
    try:
        raw = runner(prompt_for(question, cands, max_chosen), **kwargs)
    except Exception:                                        # a runner that dies is a missing model, not a crash
        return _fallback(cands, "fallback: runner failed")
    m = _JSON_RE.search(raw or "")
    if not m:
        return _fallback(cands, "fallback: top by match", "not the JSON answer shape")
    try:
        doc = json.loads(m.group(0))
    except ValueError:
        return _fallback(cands, "fallback: top by match", "not the JSON answer shape")
    if not isinstance(doc, dict) or not isinstance(doc.get("chosen"), list) or not isinstance(doc.get("basis"), str):
        return _fallback(cands, "fallback: top by match", "not the JSON answer shape")
    picked = [x for x in doc["chosen"] if isinstance(x, str) and x]
    if len(picked) != len(doc["chosen"]):
        return _fallback(cands, "fallback: top by match", "not the JSON answer shape")
    if len(picked) > max_chosen:
        return _fallback(cands, "fallback: top by match", f"more than {max_chosen} nodes")
    allowed = {c["qn"] for c in cands} | {c["node_key"] for c in cands if c.get("node_key")}
    if any(x not in allowed for x in picked):
        return _fallback(cands, "fallback: top by match", "a name outside the candidates")
    return {"chosen": picked, "basis": doc["basis"], "fallback": False, "rejected": None, "raw": raw}
