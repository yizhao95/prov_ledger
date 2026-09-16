"""ask — the read-only question entry (DP phase 2e; spec §21).

`/ledger?q=` and `provledger ask "<question>"` answer "why did this happen" and
"did we try that" from the ledger alone. The division of labour is the feature:

  code  · candidates by literal match (`locate.candidates`)
  model · picks among those candidates, nothing else (`locate.choose`)
  code  · the fact table, the absences and the scope (`facts`, `absence`, `scope`)
  model · one paragraph that may only restate the fact table, every sentence
          citing a record id (`summarize`); uncited sentences and numbers that
          are not in the table are dropped by code, and the drops are counted
  code  · the evidence card (`card`)

Nothing here writes a business table. One `ask_log` row per question, one
`ask_feedback` row per verdict — both append-only (migration 022).
"""
from __future__ import annotations

import json
import os

DASHBOARD_URL_ENV = "PROVLEDGER_DASHBOARD_URL"
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8765"


def record_ask(conn, *, project: str, question: str, candidates=None, chosen=None, facts_sha: str | None = None,
               answer: str | None = None, cites=None, scope=None, dropped=None, model: str | None = None,
               runner: str | None = None, commit: bool = True) -> int:
    """One row per question, written once. Returns ask_log.id."""
    def js(x):
        return None if x is None else json.dumps(x, ensure_ascii=False, default=str)

    cur = conn.execute(
        "INSERT INTO ask_log (project, question, candidates_json, chosen_json, facts_sha, answer, cites_json, "
        "scope_json, dropped_json, model, runner) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project, question, js(candidates), js(chosen), facts_sha, answer, js(cites), js(scope), js(dropped),
         model, runner))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def record_feedback(conn, *, ask_id: int, verdict: str, note: str | None = None, by: str = "human",
                    commit: bool = True) -> int:
    """A person's word on one answer (wrong | partial | right) — the calibration set."""
    if verdict not in ("wrong", "partial", "right"):
        raise ValueError(f"verdict must be wrong | partial | right, got {verdict!r}")
    cur = conn.execute("INSERT INTO ask_feedback (ask_id, verdict, note, by) VALUES (?, ?, ?, ?)",
                       (ask_id, verdict, note, by))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def get_ask(conn, ask_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM ask_log WHERE id = ?", (ask_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for k in ("candidates_json", "chosen_json", "cites_json", "scope_json", "dropped_json"):
        d[k[:-5]] = None
        if d.get(k):
            try:
                d[k[:-5]] = json.loads(d[k])
            except ValueError:
                d[k[:-5]] = None
    return d


# ── the pipeline: code, model, code, model, code ─────────────────────────────

def dashboard_url() -> str:
    return (os.environ.get(DASHBOARD_URL_ENV) or DEFAULT_DASHBOARD_URL).rstrip("/")


def _url(project: str, fact: dict) -> str:
    """The context triple a fact opens: (project, node, at)."""
    base = dashboard_url()
    node = fact.get("node") or ""
    kind = fact.get("kind")
    if kind == "reference" and fact.get("uri"):
        return fact["uri"]
    if kind in ("influence", "expectation", "outcome") and fact.get("plan_id"):
        return f"{base}/plan/{fact['plan_id']}"
    if kind == "change":
        return f"{base}/node/{project}/{node}?at={fact.get('run_id')}"
    if kind in ("constraints", "reasons", "rejected_paths"):
        return f"{base}/node/{project}/{node}?at={fact.get('id')}"
    return f"{base}/node/{project}/{node}"


def records(ft: dict, cites=None) -> list[dict]:
    """The cited facts, each with the URL that opens it — or, when the model said
    nothing, every fact in the table, so the reader still has the records."""
    keys = [c for c in (cites or []) if c in ft["ids"]] or list(ft["ids"])
    out = []
    for cite in keys:
        fact = ft["ids"][cite]
        label = fact.get("kind")
        if label == "reference":
            label = f"source ({fact.get('source_kind')})"
        out.append({"cite": cite, "kind": label, "node": fact.get("node"),
                    "url": _url(ft["project"], fact),
                    "text": (fact.get("text") or fact.get("label") or fact.get("claim")
                             or fact.get("event_type") or fact.get("via") or "")})
    return out


def run(conn, *, project: str, question: str, psg_db_path: str | None = None, runner=None, summary_runner=None,
        model: str | None = None, runner_name: str | None = None, lang: str = "en", record: bool = True) -> dict:
    """One question end to end. `runner` picks the nodes, `summary_runner` writes
    the paragraph (defaults to `runner`); `runner=None` is the degraded mode."""
    from . import absence as absence_mod, facts as facts_mod, locate, scope as scope_mod, summarize as summarize_mod
    from .. import psg_bridge

    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    pool = locate.candidates(conn, psg, question, project=project, limit=10 ** 9)
    cands = pool[:locate.MAX_CANDIDATES]
    truncated = {"candidates": len(pool) - len(cands)} if len(pool) > len(cands) else {}
    chosen = locate.choose(question, cands, runner=runner, model=model)
    ft = facts_mod.facts(conn, psg, chosen["chosen"], project=project)
    absences = absence_mod.absences(conn, ft)
    sc = scope_mod.scope(ft, candidates=len(pool), chosen=len(chosen["chosen"]), truncated=truncated)
    sc_line = scope_mod.line(sc, lang)
    summary = summarize_mod.summarize(question, ft, absences=absences, scope_line=sc_line,
                                      runner=summary_runner if summary_runner is not None else runner, model=model)
    facts_sha = facts_mod.sha(ft)
    doc = {"project": project, "question": question, "candidates": cands, "chosen": chosen,
           "facts": ft, "facts_text": summary["facts_text"], "facts_sha": facts_sha,
           "absences": absences, "scope": sc, "scope_line": sc_line,
           "answer": summary["answer"], "sentences": summary["sentences"], "cites": summary["cites"],
           "dropped": summary["dropped"], "dropped_detail": summary["dropped_detail"],
           "degraded": summary["degraded"], "note": summary["note"], "model": model,
           "runner": runner_name or ("none" if runner is None else "stub"), "lang": lang}
    doc["records"] = records(ft, summary["cites"])
    doc["ask_id"] = record_ask(conn, project=project, question=question, candidates=cands, chosen=chosen,
                               facts_sha=facts_sha, answer=summary["answer"], cites=summary["cites"], scope=sc,
                               dropped=summary["dropped"], model=model, runner=doc["runner"]) if record else None
    return doc


def render_text(doc: dict) -> str:
    """The terminal rendering of one answer: what it says, what is missing, how far we looked."""
    out = [f"Q: {doc['question']}", ""]
    if doc["degraded"]:
        out.append(doc["note"] or "summary unavailable: no model")
    else:
        out += doc["sentences"] or ["(nothing survived the checks)"]
        if doc.get("note"):
            out.append(f"({doc['note']})")
    if doc["absences"]:
        out += ["", "Absences"] + [f"  {a['text']}" for a in doc["absences"]]
    out += ["", doc["scope_line"]]
    if doc["degraded"] or not doc["answer"]:
        out += ["", doc["facts_text"]]
    if doc["records"]:
        out += ["", "Records"]
        for r in doc["records"]:
            text = (r["text"] or "").replace("\n", " ")
            out.append(f"  [{r['cite']}] {r['kind']} · {r['node']} · {r['url']}")
            if text:
                out.append(f"      {text[:160]}")
    if doc.get("ask_id"):
        out += ["", f"ask id {doc['ask_id']} · say `provledger ask feedback {doc['ask_id']} wrong` if this is wrong"]
    return "\n".join(out)


def as_json(doc: dict) -> dict:
    """The machine-readable answer — the fact table as text, not as a nested blob."""
    keep = ("ask_id", "project", "question", "answer", "sentences", "cites", "scope", "scope_line",
            "facts_text", "facts_sha", "dropped", "dropped_detail", "degraded", "note", "model", "runner",
            "records", "lang")
    out = {k: doc.get(k) for k in keep}
    out["absences"] = doc.get("absences")
    out["candidates"] = [{k: c[k] for k in ("qn", "node_key", "why", "score")} for c in doc.get("candidates", [])]
    out["chosen"] = doc.get("chosen")
    return out
