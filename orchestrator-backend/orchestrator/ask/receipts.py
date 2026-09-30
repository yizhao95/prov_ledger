"""ask.receipts — the material a reply would need, and nothing more (spec §8, G).

Someone challenges a decision. `provledger receipts "<what they said>"` assembles
the record behind it and stops. It does not write the reply: writing a courteous
workplace reply is the session model's native ability — people already paste
context into a model and get a good reply back. What the model lacks is not
skill, it is **material with sources attached** (spec §8.0, the 2026-09-30
correction, which deleted the register selector, the tone classifier and the
citation-checking pipeline that used to live here — there is no generated text
to check).

So this module is a **fourth renderer over the same three stages** `ask` already
runs, not a second pipeline:

  `locate.candidates` / `locate.choose`  which nodes the challenge is about
  `facts.facts`                          the fact table and its cite namespace
  `absence.absences`                     what is missing, computed by code
  `scope.scope` / `scope.line`           how far the search looked

and it differs from `ask` only in the arrangement: a **timeline**, because the
order is what answers "wasn't it the other way round"; every line ending in the
id it rests on; absences stated as absences; and a closing instruction that is
part of the output, because it is what keeps the model on the record.

Two things it does not do, both inherited:

  · **no model call.** `choose` runs with `runner=None`, its code-only path.
  · **no write of any kind** — not even a `read_hit`, and no `ask_log` row:
    there is no answer to log, and a question is not a plan (`facts`, `ask`).
"""
from __future__ import annotations

TIMELINE_HEADER = "What the record holds"
ABSENCE_HEADER = "Not on the record"
SCOPE_HEADER = "Searched"
ASKED_HEADER = "Someone asked:"

# The instruction is part of the output, not advice in the docs: the material is
# handed to a model that will write the reply, and this is the fence around it.
CLOSING = ("Every claim in your reply must map to one of the lines above. Anything those lines\n"
           "do not support does not go in the reply.")

# "nothing recorded" is a different claim from "nothing found", and the product
# says so in its own words rather than leaving the reader to infer it.
ABSENCE_FOOTER = ('Each line above is "nothing recorded", never "nothing found": '
                  "the range that was searched is below.")
NO_ABSENCE = "No absence could be computed: there is no record in range to compare against."
NO_RECORD = ("nothing in the ledger matched this, so there is nothing to hand over — "
             "which is itself only true of the range below")
UNDATED = "(no date recorded)"

TEXT_CAP = 160          # one line per fact, so the quotation is capped, never wrapped
ROLE_LABEL = {"constraints": "constraint", "reasons": "reason", "rejected_paths": "rejected path"}


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


def _clip(text: str) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP - 1].rstrip() + "…"


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


def timeline(conn, ft: dict) -> list[dict]:
    """Every fact in the table as one dated line, oldest first.

    Each entry is `{at, cite, kind, node, text}`; `at` is None when the record
    carries no time, and those sort to the end rather than being dropped — an
    undated record is still a record.
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
        for r in node.get("changes") or []:
            add(r.get("at"), r["cite"], "change", qn, f"the graph recorded {r.get('event_type')} in run {r.get('run_id')}")
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


# ── assemble: the three stages, then the arrangement ─────────────────────────

def assemble(conn, *, project: str, challenge: str, psg_db_path: str | None = None, lang: str = "en") -> dict:
    """The material behind one challenge. Reads only; asks no model."""
    from . import absence as absence_mod, facts as facts_mod, locate, scope as scope_mod
    from .. import psg_bridge

    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    pool = locate.candidates(conn, psg, challenge, project=project, limit=10 ** 9)
    cands = pool[:locate.MAX_CANDIDATES]
    truncated = {"candidates": len(pool) - len(cands)} if len(pool) > len(cands) else {}
    # runner=None is `choose`'s code-only path: the top matches by score, and it
    # says so in `basis`. No model is called here at all (spec §8.1).
    chosen = locate.choose(challenge, cands, runner=None)
    ft = facts_mod.facts(conn, psg, chosen["chosen"], project=project)
    absences = absence_mod.absences(conn, ft)
    sc = scope_mod.scope(ft, candidates=len(pool), chosen=len(chosen["chosen"]), truncated=truncated)
    return {"project": project, "challenge": challenge, "lang": lang,
            "candidates": cands, "chosen": chosen, "facts": ft, "facts_sha": facts_mod.sha(ft),
            "timeline": timeline(conn, ft), "absences": absences,
            "scope": sc, "scope_line": scope_mod.line(sc, lang), "closing": CLOSING}


# ── rendering ────────────────────────────────────────────────────────────────

def _line(entry: dict) -> str:
    return f"  {(entry['at'] or UNDATED):<18}  {entry['text']}  [{entry['cite']}]"


def render_text(doc: dict) -> str:
    """The plain-text material: what was said, what the record holds, what is
    missing, how far the search looked, and the instruction that fences it."""
    absences = ([f"  {a['text']}" for a in doc["absences"]] + [f"  {ABSENCE_FOOTER}"]
                if doc["absences"] else [f"  {NO_ABSENCE}"])
    out = [ASKED_HEADER, f'  "{doc["challenge"].strip()}"']
    out += ["", TIMELINE_HEADER] + ([_line(e) for e in doc["timeline"]] or [f"  {NO_RECORD}"])
    out += ["", ABSENCE_HEADER] + absences
    out += ["", SCOPE_HEADER, f"  {doc['scope_line']}"]
    out += ["", "---", CLOSING]
    return "\n".join(out)


def as_json(doc: dict) -> dict:
    """The same content, machine-readable — the fact table as text, not a blob."""
    from . import facts as facts_mod

    keep = ("project", "challenge", "lang", "timeline", "absences", "scope", "scope_line", "facts_sha", "closing")
    out = {k: doc.get(k) for k in keep}
    out["facts_text"] = facts_mod.render(doc["facts"])
    out["chosen"] = doc.get("chosen")
    out["candidates"] = [{k: c[k] for k in ("qn", "node_key", "why", "score")} for c in doc.get("candidates", [])]
    return out
