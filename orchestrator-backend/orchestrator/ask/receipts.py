"""ask.receipts — the material a reply would need, and nothing more (spec §8, G).

Someone challenges a decision. `provledger receipts …` assembles the record
behind it and stops. It does not write the reply: writing a courteous workplace
reply is the session model's native ability — people already paste context into
a model and get a good reply back. What the model lacks is not skill, it is
**material with sources attached** (spec §8.0, the 2026-09-30 correction, which
deleted the register selector, the tone classifier and the citation-checking
pipeline that used to live here — there is no generated text to check).

So this module is a **fourth renderer over the same three stages** `ask` already
runs, not a second pipeline:

  `locate.candidates`                    which nodes the challenge might be about
  `facts.facts`                          the fact table and its cite namespace
  `absence.absences`                     what is missing, computed by code
  `scope.scope` / `scope.line`           how far the search looked

and it differs from `ask` only in the arrangement: a **timeline**, because the
order is what answers "wasn't it the other way round"; every line ending in the
id it rests on; absences stated as absences; and a closing instruction that is
part of the output, because it is what keeps the model on the record.

## Two reads, and the model between them (spec §8.8)

`locate.choose` is deliberately absent from the list above. It used to be there,
called as `choose(…, runner=None)`, which read like "no model is called" and
meant something else entirely: `choose`'s code-only path, which returns the top
`locate.FALLBACK_TOP` candidates by match score. Nobody was choosing — a scoring
function was, and the score counts how often the question's words appear, so the
most-*mentioned* node won rather than the most relevant one. Measured against
this project's own ledger, "why is the review timeout 4800 seconds?" put
`ask.summarize.review` first: six points for having "review" in its name, two
for one text hit, nothing whatever to do with timeouts.

The choosing step therefore left Python. There are now two reads with the
session's own model in between:

  `candidates(…)`  the nodes the question's words touch, each with the matcher
                   that found it and its score, the cap named and the cut
                   stated. The score may ORDER this list; it must not be the
                   thing that decides.
  `facts(…)`       the record behind the names that came back — the fact table,
                   the timeline, the absences, the scope line.

`assemble(…)` keeps the one-shot form working, because it is documented and
people use it, but it is honest about what it is: it picks by score and says so
in the output, in words, next to the two commands that do better.

Two things this module does not do, both inherited and both now pinned by tests:

  · **no model call.** No receipts path takes a runner, reaches `ask.runner` or
    reaches `claude_arbiter` — the model that writes the reply is the one
    already in the room (spec §8.8).
  · **no write of any kind** — not even a `read_hit`, and no `ask_log` row:
    there is no answer to log, and a question is not a plan (`facts`, `ask`).
"""
from __future__ import annotations

TIMELINE_HEADER = "What the record holds"
ABSENCE_HEADER = "Not on the record"
SCOPE_HEADER = "Searched"
ASKED_HEADER = "Someone asked:"
NODES_HEADER = "Nodes in this material"
CANDIDATES_HEADER = "Candidate nodes"

# How many candidates the first read hands over. The old number was
# `locate.FALLBACK_TOP` = 5, which is what a scoring function can defend and not
# what a reader can choose from: the pool for one real challenge was 109 nodes,
# cut to 40 and then to 5 before anything that could judge relevance ever saw
# the list. A model reads this list, so the cap belongs near the model's
# context, not near a page of terminal — and either way it is stated, never
# silent (`cap_line`).
CANDIDATE_CAP = 40

# The instruction is part of the output, not advice in the docs: the material is
# handed to a model that will write the reply, and this is the fence around it.
CLOSING = ("Every claim in your reply must map to one of the lines above. Anything those lines\n"
           "do not support does not go in the reply.")

# The first read ends in a question for the reader, not in a ranking. Saying so
# is the whole point of splitting the command in two.
CANDIDATES_CLOSING = (
    "Pick the nodes this challenge is actually about and ask for their record:\n"
    "  provledger receipts facts <qualified name> [<qualified name> …]\n"
    "The score above counts how often the question's words appear. It orders this list so\n"
    "that something is on top; it is not a judgement about relevance, and a node the\n"
    "question never named can still be the one that answers it.")

# `assemble()`'s notice. A one-shot form that quietly used the score is how the
# old prescription went wrong; the fix is not to remove the form but to make it
# say what it did.
UNCHOSEN = (
    "These nodes were picked by match score, not by judgement: nobody read the candidate\n"
    "list. The score counts how often the question's words appear, so the node mentioned\n"
    "most often wins rather than the node the question is about. For a pick that is a pick:\n"
    '  provledger receipts candidates "<what they said>"   the list, with why each matched\n'
    "  provledger receipts facts <qualified name> …        the record behind the ones you chose")

# "nothing recorded" is a different claim from "nothing found", and the product
# says so in its own words rather than leaving the reader to infer it.
ABSENCE_FOOTER = ('Each line above is "nothing recorded", never "nothing found": '
                  "the range that was searched is below.")
NO_ABSENCE = "No absence could be computed: there is no record in range to compare against."
NO_RECORD = ("nothing in the ledger matched this, so there is nothing to hand over — "
             "which is itself only true of the range below")
NO_CANDIDATE = "no node in this project matched any word in that sentence"
UNDATED = "(no date recorded)"

BY_SCORE = "picked by match score"
BY_CALLER = "named on the command line"

# `ask.facts` labels a node `existing` / `new` / `not in graph` / `graph
# unavailable`, and those words are its contract with `/ledger`, so they are
# reproduced rather than renamed. What they mean is not obvious to someone
# reading a challenge, and "new" in particular reads like good news when it
# means "nothing under this name has ever been analysed" — so the status is
# printed with a gloss beside it, never instead of it.
STATUS_GLOSS = {
    "existing": "in the graph",
    "new": "not in the graph, so nothing has been analysed under this name",
    "not in graph": "not in the graph",
    "graph unavailable": "the state graph could not be read, so this may be wrong",
}

TEXT_CAP = 160          # one line per fact, so the quotation is capped, never wrapped
WHY_CAP = 300           # the matcher trail can be long; it is the reason a node is here
ROLE_LABEL = {"constraints": "constraint", "reasons": "reason", "rejected_paths": "rejected path"}


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


# ── the one read this renderer adds ──────────────────────────────────────────

def _sources(conn, cites: list[str]) -> dict[str, dict]:
    """When each source happened and whether it still opens.

    The fact table carries a source's cite, kind, label and uri — enough for a
    citation, not enough for a **timeline**: a source with no date cannot take
    its place in the order, and "linked" with no check date reads like a promise.
    Both columns are read straight off `reference`, never re-derived, and this is
    a SELECT like every other line here.
    """
    ids = []
    for cite in cites:
        try:
            ids.append(int(cite[2:]))
        except ValueError:
            continue
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    return {f"#r{r[0]}": {"occurred_at": r[1], "verifiability": r[2], "last_checked": r[3]}
            for r in conn.execute(f"SELECT id, occurred_at, verifiability, last_checked FROM reference "
                                  f"WHERE id IN ({ph})", ids)}


# ── the timeline ─────────────────────────────────────────────────────────────

def _at(value) -> str | None:
    """A stamp that sorts and reads: `2026-08-14T09:12:00+00:00` and
    `2026-08-14 09:12:00` are the same moment and must land next to each other,
    and seconds are noise in a timeline. A date-only record stays a date."""
    if not value:
        return None
    return str(value).replace("T", " ")[:16]


def _clip(text: str, cap: int = TEXT_CAP) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


def _source_text(ref: dict, extra: dict) -> str:
    verifiability = extra.get("verifiability") or ("linked" if ref.get("uri") else "unreachable")
    checked = f"checked {str(extra['last_checked'])[:10]}" if extra.get("last_checked") else "never checked"
    if verifiability != "linked":
        checked = "nothing to open"
    return f'source · {ref.get("kind")} · "{_clip(ref.get("label") or "")}" · {verifiability}, {checked}'


def _record_text(kind: str, node: str, r: dict) -> str:
    who = f"{r.get('tier') or '?'} by {r.get('recorded_by') or 'unknown'}"
    shown = f"shown {r.get('shown', 0)}, adopted by {len(r.get('adopted_by') or [])}"
    body = f' · "{_clip(r.get("text") or "")}"' if r.get("text") else ""
    return f"{ROLE_LABEL[kind]} on {node} · {who}{body} · {shown}"


def graph_events_line(count: int) -> str:
    """The one line that stands in for the state graph's own bookkeeping.

    Measured on a real challenge, 34 of 63 lines of material were `the graph
    recorded node_changed in run N`. Those rows are the graph's record of
    ITSELF — that a node's signature differed between two analysis runs — not
    anything anyone said about the decision being challenged, and they crowded
    out the reasons because their dates are early (spec §8.8 item 3).

    They leave the material. They do not leave the output: a cut in this project
    is stated, so the count rides at the foot of the timeline. Anyone who wants
    the events themselves has `provledger why <node>`, which is where the
    graph's history belongs.
    """
    return (f"Left out: {_n(count, 'graph event')} in range — the state graph's record of itself "
            f"(node_added, signature_changed, …) is bookkeeping, not testimony. `provledger why "
            f"<node>` shows them.")


def timeline(conn, ft: dict) -> list[dict]:
    """Every fact in the table as one dated line, oldest first.

    Each entry is `{at, cite, kind, node, text}`; `at` is None when the record
    carries no time, and those sort to the end rather than being dropped — an
    undated record is still a record. The graph's own change events are the one
    thing excluded, and `graph_events_line` states how many (spec §8.8 item 3).
    """
    out: list[dict] = []
    seen: set[str] = set()

    def add(at, cite: str, kind: str, node: str, text: str) -> None:
        if cite in seen:                       # one source hung on two records is one source
            return
        seen.add(cite)
        out.append({"at": _at(at), "cite": cite, "kind": kind, "node": node, "text": text})

    refs: list[tuple] = []
    for node in ft.get("nodes") or []:
        qn = node["qn"]
        for kind in ("constraints", "reasons", "rejected_paths"):
            for r in node.get(kind) or []:
                for ref in r.get("references") or []:
                    refs.append((ref, qn))
                add(r.get("occurred_at"), r["cite"], kind, qn, _record_text(kind, qn, r))
        for r in node.get("influence") or []:
            plan = r.get("plan_id") or "an unnamed plan"
            add(r.get("at"), r["cite"], "influence", qn,
                f"{plan} was shown #{r.get('reason_id')} and adopted it, via {r.get('via')}")
        for r in node.get("expectations") or []:
            add(r.get("created_at"), r["cite"], "expectation", qn,
                f'{r.get("plan_id")} expected "{_clip(r.get("claim") or "")}" · channel {r.get("channel")}')
            o = r.get("outcome")
            if o:
                add(o.get("observed_at"), o["cite"], "outcome", qn,
                    f"outcome of {r['cite']} · {o.get('kind')} · {o.get('tier')}")
        for r in node.get("values") or []:
            unit = f" {r['unit']}" if r.get("unit") else ""
            add(r.get("observed_at"), r["cite"], "value", qn, f"{r.get('name')} measured at {r.get('value')}{unit}")

    extras = _sources(conn, [ref["cite"] for ref, _ in refs])
    for ref, qn in refs:
        add(extras.get(ref["cite"], {}).get("occurred_at"), ref["cite"], "reference", qn,
            _source_text(ref, extras.get(ref["cite"], {})))
    # undated last: it keeps its place in the record without claiming a place in time
    out.sort(key=lambda e: (e["at"] is None, e["at"] or "", e["cite"]))
    return out


def graph_events(ft: dict) -> int:
    """How many of the fact table's rows are the graph's bookkeeping about
    itself — counted off the table, so the line that reports the cut cannot
    drift from what was cut."""
    return sum(len(n.get("changes") or []) for n in ft.get("nodes") or [])


# ── read one · the candidates, to be chosen FROM ─────────────────────────────

def cap_line(matched: int, shown: int, cap: int, lang: str = "en") -> str:
    """How wide the list is and what the cap took off it.

    `scope.line` does this for the second read; the first read needs its own,
    because a candidate list that silently stops at N is exactly the failure
    §8.8 describes: the reader cannot tell a short list from a truncated one,
    and picks from what they were shown believing it was everything.
    """
    cut = max(0, matched - shown)
    if lang == "zh":
        tail = f"裁掉 {cut} 个未显示" if cut else "无裁剪"
        return (f"匹配到 {matched} 个节点；显示 {shown} 个（上限 {cap}），{tail}。"
                f"分数只负责排序，不负责挑选。")
    tail = f"{cut} not shown" if cut else "nothing cut"
    return (f"Matched {_n(matched, 'node')}; showing {shown} (cap {cap}), {tail}. "
            f"The score orders this list; it does not choose.")


def candidates(conn, *, project: str, challenge: str, psg_db_path: str | None = None,
               cap: int = CANDIDATE_CAP, lang: str = "en") -> dict:
    """The nodes the challenge's own words touch — the list a model picks from.

    Broad and honest: every candidate keeps the matcher that found it (`why`) and
    its match score, the whole matched pool is counted, and the cap is reported.
    Nothing here decides anything; deciding is the next step, and it happens
    outside this process (spec §8.8).
    """
    from . import locate
    from .. import psg_bridge

    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    pool = locate.candidates(conn, psg, challenge, project=project, limit=10 ** 9)
    shown = pool[:max(0, int(cap))]
    truncated = {"candidates": len(pool) - len(shown)} if len(pool) > len(shown) else {}
    return {"project": project, "challenge": challenge, "lang": lang, "cap": int(cap),
            "matched": len(pool), "candidates": shown, "truncated": truncated,
            "cap_line": cap_line(len(pool), len(shown), int(cap), lang),
            "closing": CANDIDATES_CLOSING}


# ── read two · the record behind the names that came back ────────────────────

def facts(conn, *, project: str, chosen: list[str], psg_db_path: str | None = None,
          challenge: str | None = None, lang: str = "en", picked_by: str = "caller",
          matched: int | None = None, truncated: dict | None = None) -> dict:
    """The material for nodes somebody already chose. Reads only; asks no model.

    `chosen` is taken as given: this read runs no matcher and makes no pick, so
    a name the graph does not know comes back with its status said out loud
    rather than dropped. `picked_by` records who chose — the caller, or (from
    `assemble`) the match score — and the renderer prints it, because "which
    nodes these are" is the step where the old command went wrong.
    """
    from . import absence as absence_mod, facts as facts_mod, scope as scope_mod
    from .. import psg_bridge

    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    names = [n for n in (chosen or []) if n]
    ft = facts_mod.facts(conn, psg, names, project=project)
    absences = absence_mod.absences(conn, ft)
    sc = scope_mod.scope(ft, candidates=len(names) if matched is None else matched,
                         chosen=len(names), truncated=truncated or {})
    return {"project": project, "challenge": challenge, "lang": lang, "picked_by": picked_by,
            "chosen_names": names, "facts": ft, "facts_sha": facts_mod.sha(ft),
            "timeline": timeline(conn, ft), "graph_events": graph_events(ft),
            "absences": absences, "scope": sc, "scope_line": scope_mod.line(sc, lang),
            "closing": CLOSING}


# ── the one-shot form: still here, and honest about what it is ───────────────

def assemble(conn, *, project: str, challenge: str, psg_db_path: str | None = None, lang: str = "en") -> dict:
    """`provledger receipts "<challenge>"` — both reads in one, picked by score.

    Kept because it is documented and people run it. It takes the top
    `locate.FALLBACK_TOP` candidates by match score, which is what the old
    `choose(…, runner=None)` did all along; the difference is that the output now
    says so (`UNCHOSEN`) and names the two-step form. No model is asked here or
    anywhere else in this module.
    """
    from . import locate

    first = candidates(conn, project=project, challenge=challenge, psg_db_path=psg_db_path, lang=lang)
    picked = [c["qn"] for c in first["candidates"][:locate.FALLBACK_TOP]]
    doc = facts(conn, project=project, chosen=picked, psg_db_path=psg_db_path, challenge=challenge,
                lang=lang, picked_by="score", matched=first["matched"], truncated=first["truncated"])
    doc["candidates"] = first["candidates"]
    doc["cap"] = first["cap"]
    doc["matched"] = first["matched"]
    doc["cap_line"] = first["cap_line"]
    return doc


# ── rendering ────────────────────────────────────────────────────────────────

def _line(entry: dict) -> str:
    return f"  {(entry['at'] or UNDATED):<18}  {entry['text']}  [{entry['cite']}]"


def _asked(doc: dict) -> list[str]:
    return [ASKED_HEADER, f'  "{(doc.get("challenge") or "").strip()}"', ""] if doc.get("challenge") else []


def render_candidates(doc: dict) -> str:
    """The first read as text: what they said, every node their words touch, the
    matcher that found each one, and the cap. It ends in an instruction to pick,
    because picking is the next step and it is not this command's."""
    out = _asked(doc)
    out += [CANDIDATES_HEADER]
    for c in doc["candidates"]:
        out.append(f"  {c['qn']} · score {c.get('score', 0)}")
        out.append(f"      {_clip(c.get('why') or 'no matcher recorded', WHY_CAP)}")
        if c.get("latest"):
            out.append(f"      latest record: {_clip(c['latest'])}")
    if not doc["candidates"]:
        out.append(f"  {NO_CANDIDATE}")
    out.append(f"  {doc['cap_line']}")
    out += ["", "---", CANDIDATES_CLOSING]
    return "\n".join(out)


def render_text(doc: dict) -> str:
    """The material: what was said, which nodes it is about and who chose them,
    what the record holds, what is missing, how far the search looked, and the
    instruction that fences it."""
    absences = ([f"  {a['text']}" for a in doc["absences"]] + [f"  {ABSENCE_FOOTER}"]
                if doc["absences"] else [f"  {NO_ABSENCE}"])
    how = BY_SCORE if doc.get("picked_by") == "score" else BY_CALLER
    nodes = [f"  {n['qn']} · {n['status']} ({STATUS_GLOSS.get(n['status'], n['status'])}) · {how}"
             for n in doc["facts"].get("nodes") or []]

    out = _asked(doc)
    out += [NODES_HEADER] + (nodes or [f"  {NO_CANDIDATE}"])
    if doc.get("picked_by") == "score":
        out += ["", UNCHOSEN]
    out += ["", TIMELINE_HEADER] + ([_line(e) for e in doc["timeline"]] or [f"  {NO_RECORD}"])
    # Outside the timeline section, deliberately: every line under that heading
    # ends in the id it rests on, and this line rests on no record — it is the
    # statement of a cut, not a fact. It still prints, because a silent cut is
    # the thing this project exists to prevent.
    if doc.get("graph_events"):
        out += ["", graph_events_line(doc["graph_events"])]
    out += ["", ABSENCE_HEADER] + absences
    out += ["", SCOPE_HEADER, f"  {doc['scope_line']}"]
    out += ["", "---", CLOSING]
    return "\n".join(out)


def _slim(c: dict) -> dict:
    return {k: c.get(k) for k in ("qn", "node_key", "why", "score")}


def candidates_as_json(doc: dict) -> dict:
    """The first read, machine-readable — the same four fields a reader sees."""
    keep = ("project", "challenge", "lang", "cap", "matched", "truncated", "cap_line", "closing")
    out = {k: doc.get(k) for k in keep}
    out["candidates"] = [_slim(c) for c in doc.get("candidates") or []]
    return out


def as_json(doc: dict) -> dict:
    """The same content, machine-readable — the fact table as text, not a blob."""
    from . import facts as facts_mod

    keep = ("project", "challenge", "lang", "picked_by", "chosen_names", "timeline", "absences",
            "scope", "scope_line", "facts_sha", "graph_events", "closing")
    out = {k: doc.get(k) for k in keep}
    out["graph_events_line"] = graph_events_line(doc["graph_events"]) if doc.get("graph_events") else None
    out["facts_text"] = facts_mod.render(doc["facts"])
    if doc.get("picked_by") == "score":                # the one-shot form carries its own notice
        out["unchosen"] = UNCHOSEN
        out["cap"] = doc.get("cap")
        out["matched"] = doc.get("matched")
        out["cap_line"] = doc.get("cap_line")
        out["candidates"] = [_slim(c) for c in doc.get("candidates") or []]
    return out
