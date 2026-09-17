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
`ask_feedback` row per verdict — both append-only (migration 023).
"""
from __future__ import annotations

import json
import os

DASHBOARD_URL_ENV = "PROVLEDGER_DASHBOARD_URL"
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8765"
# The budget a person waits through. The first real page load took 185 s, all of
# it in one query, and nothing on the page said so — so the budget is a test and
# the measurement is printed with the answer (`scope._cost`).
BUDGET_S = 3.0


def _has_column(conn, table: str, column: str) -> bool:
    """`runner_detail` arrived in migration 029; a page that logs into a ledger
    opened by an older build must still log, minus the column it does not have."""
    try:
        return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))
    except Exception:
        return False


def record_ask(conn, *, project: str, question: str, candidates=None, chosen=None, facts_sha: str | None = None,
               answer: str | None = None, cites=None, scope=None, dropped=None, model: str | None = None,
               runner: str | None = None, elapsed_ms: int | None = None, runner_detail=None,
               commit: bool = True) -> int:
    """One row per question, written once. Returns ask_log.id."""
    def js(x):
        return None if x is None else json.dumps(x, ensure_ascii=False, default=str)

    cols = ["project", "question", "candidates_json", "chosen_json", "facts_sha", "answer", "cites_json",
            "scope_json", "dropped_json", "model", "runner", "elapsed_ms"]
    vals = [project, question, js(candidates), js(chosen), facts_sha, answer, js(cites), js(scope), js(dropped),
            model, runner, elapsed_ms]
    if _has_column(conn, "ask_log", "runner_detail"):
        cols.append("runner_detail")
        vals.append(js(runner_detail))
    cur = conn.execute(f"INSERT INTO ask_log ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
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
    for k in ("candidates_json", "chosen_json", "cites_json", "scope_json", "dropped_json", "runner_detail"):
        name = k[:-5] if k.endswith("_json") else k   # runner_detail is its own name, not a _json alias
        raw = d.get(k)
        d[name] = None
        if raw:
            try:
                d[name] = json.loads(raw)
            except ValueError:
                d[name] = None
    return d


# ── the pipeline: code, model, code, model, code ─────────────────────────────

def dashboard_url() -> str:
    return (os.environ.get(DASHBOARD_URL_ENV) or DEFAULT_DASHBOARD_URL).rstrip("/")


def _url(project: str, fact: dict) -> str:
    """The context triple a fact opens: (project, node, at)."""
    base = dashboard_url()
    node = fact.get("node") or ""
    kind = fact.get("kind")
    if kind == "reference":
        # a source you can open goes to the source; one you cannot goes to the
        # record that cites it, because a link with no anchor reads like a dead end
        return fact["uri"] if fact.get("uri") else f"{base}/node/{project}/{node}?at=reason:{fact.get('reason_id')}"
    if kind in ("influence", "expectation", "outcome") and fact.get("plan_id"):
        return f"{base}/plan/{fact['plan_id']}"
    # DP phase 2d typed the anchor: `at` says WHAT it points at, so a record id
    # is never handed to a run lookup (queries.parse_at / AT_PREFIXES).
    if kind == "change":
        return f"{base}/node/{project}/{node}?at=run:{fact.get('run_id')}"
    if kind in ("constraints", "reasons", "rejected_paths"):
        return f"{base}/node/{project}/{node}?at=reason:{fact.get('id')}"
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


RAW_HEAD = 2000                 # how much of a model answer ask_log keeps verbatim


def _raw_head(raw) -> str | None:
    return None if not raw else str(raw)[:RAW_HEAD]


def runner_trace(chosen: dict, summary: dict, candidates: int) -> dict:
    """What actually happened at each of the two model calls — the outcome, the
    note the reader was given, the rc / stderr head / wall time the runner
    reported, and the head of the raw answer. Written to `ask_log.runner_detail`
    (migration 029), append-only, because "summary unavailable" without a reason
    is the bug this exists to prevent."""
    return {"candidates": candidates,
            "choose": {"outcome": chosen.get("outcome") or "ok", "fallback": bool(chosen.get("fallback")),
                       "rejected": chosen.get("rejected"), "basis": chosen.get("basis"),
                       "detail": chosen.get("runner_detail") or {}, "raw": _raw_head(chosen.get("raw"))},
            "summarize": {"outcome": summary.get("degraded_reason") or "ok", "note": summary.get("note"),
                          "detail": summary.get("runner_detail") or {}, "raw": _raw_head(summary.get("raw"))}}


def run(conn, *, project: str, question: str, psg_db_path: str | None = None, runner=None, summary_runner=None,
        model: str | None = None, runner_name: str | None = None, lang: str = "en", record: bool = True,
        timeout_s: float | None = None) -> dict:
    """One question end to end. `runner` picks the nodes, `summary_runner` writes
    the paragraph (defaults to `runner`); `runner=None` is the degraded mode.

    A runner may return the text or `(text, detail)` — see `ask.runner`. When it
    does not answer, the note says WHICH of the five things happened and the
    detail is logged; it never says "no model" about a model that was there."""
    import time

    from . import absence as absence_mod, facts as facts_mod, locate, scope as scope_mod, summarize as summarize_mod
    from .. import psg_bridge

    started = time.perf_counter()
    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    pool = locate.candidates(conn, psg, question, project=project, limit=10 ** 9)
    cands = pool[:locate.MAX_CANDIDATES]
    truncated = {"candidates": len(pool) - len(cands)} if len(pool) > len(cands) else {}
    chosen = locate.choose(question, cands, runner=runner, model=model, timeout_s=timeout_s)
    ft = facts_mod.facts(conn, psg, chosen["chosen"], project=project)
    absences = absence_mod.absences(conn, ft)
    sc = scope_mod.scope(ft, candidates=len(pool), chosen=len(chosen["chosen"]), truncated=truncated)
    sc_line = scope_mod.line(sc, lang)
    summary = summarize_mod.summarize(question, ft, absences=absences, scope_line=sc_line,
                                      runner=summary_runner if summary_runner is not None else runner, model=model,
                                      timeout_s=timeout_s, candidates=len(cands), lang=lang)
    facts_sha = facts_mod.sha(ft)
    trace = runner_trace(chosen, summary, len(cands))
    # the cost of the answer rides with it: a tool must not get slower in silence
    sc["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    doc = {"project": project, "question": question, "candidates": cands, "chosen": chosen,
           "elapsed_ms": sc["elapsed_ms"], "scope_line_timed": scope_mod.line(sc, lang),
           "facts": ft, "facts_text": summary["facts_text"], "facts_sha": facts_sha,
           "absences": absences, "scope": sc, "scope_line": sc_line,
           "answer": summary["answer"], "sentences": summary["sentences"], "cites": summary["cites"],
           "dropped": summary["dropped"], "dropped_detail": summary["dropped_detail"],
           "degraded": summary["degraded"], "degraded_reason": summary.get("degraded_reason"),
           "note": summary["note"], "runner_detail": trace, "model": model,
           "runner": runner_name or ("none" if runner is None else "stub"), "lang": lang}
    doc["records"] = records(ft, summary["cites"])
    doc["ask_id"] = record_ask(conn, project=project, question=question, candidates=cands, chosen=chosen,
                               facts_sha=facts_sha, answer=summary["answer"], cites=summary["cites"], scope=sc,
                               dropped=summary["dropped"], model=model, runner=doc["runner"],
                               elapsed_ms=sc["elapsed_ms"], runner_detail=trace) if record else None
    return doc


def render_text(doc: dict) -> str:
    """The terminal rendering of one answer: what it says, what is missing, how far we looked."""
    out = [f"Q: {doc['question']}", ""]
    if doc["degraded"]:
        from .summarize import NO_MODEL_NOTE
        out.append(doc["note"] or NO_MODEL_NOTE)
    else:
        out += doc["sentences"] or ["(nothing survived the checks)"]
        if doc.get("note"):
            out.append(f"({doc['note']})")
    if doc["absences"]:
        out += ["", "Absences"] + [f"  {a['text']}" for a in doc["absences"]]
    out += ["", doc.get("scope_line_timed") or doc["scope_line"]]
    if doc["degraded"] or not doc["answer"]:
        out += ["", doc["facts_text"]]
    if doc["records"]:
        out += ["", "Records"]
        for r in doc["records"]:
            text = (r["text"] or "").replace("\n", " ")
            out.append(f"  [{r['cite']}] {r['kind']} · {r['node']} · {r['url']}")
            if text:
                out.append(f"      {text[:160]}")
    if doc.get("facts_changed"):
        out += ["", "⚠ the fact table changed between the question and this answer "
                    f"({(doc.get('facts_sha_asked') or '')[:12]} → {(doc.get('facts_sha') or '')[:12]}): "
                    "the sentences above were checked against the newer table, not the one they were written from"]
    if doc.get("ask_id"):
        out += ["", f"ask id {doc['ask_id']}" + (f" · answer v{doc['version']}" if doc.get("version") else "")
                + f" · say `provledger ask feedback {doc['ask_id']} wrong` if this is wrong"]
        out += ["", "Next"] + [f"  {c}" for c in next_commands(doc)]
    return "\n".join(out)


def as_json(doc: dict) -> dict:
    """The machine-readable answer — the fact table as text, not as a nested blob."""
    keep = ("ask_id", "project", "question", "answer", "sentences", "cites", "scope", "scope_line",
            "facts_text", "facts_sha", "dropped", "dropped_detail", "degraded", "degraded_reason", "note",
            "runner_detail", "model", "runner", "records", "lang")
    out = {k: doc.get(k) for k in keep}
    out["absences"] = doc.get("absences")
    out["candidates"] = [{k: c[k] for k in ("qn", "node_key", "why", "score")} for c in doc.get("candidates", [])]
    out["chosen"] = doc.get("chosen")
    return out


# ── the session path: /ledger as a slash command (DP phase 2e, Task 6) ───────
# Inside a Claude Code session there is already a model in the room, so the
# skill hands it the computed fact table and asks for a draft. That changes WHO
# writes the sentence and nothing else: `submit` reads the draft back through
# the same `summarize.review` the headless path uses, deletes what does not cite
# and what carries a number the table never stated, and counts every deletion.
# A draft that is never checked is a model talking to itself.

def rebuild(conn, ask_id: int, *, psg_db_path: str | None = None, lang: str = "en") -> dict:
    """The fact table, absences and scope of a logged question, exactly as they
    were computed when it was asked — a draft is checked against what it saw."""
    from . import absence as absence_mod, facts as facts_mod, scope as scope_mod

    row = get_ask(conn, ask_id)
    if row is None:
        raise ValueError(f"no logged question with ask id {ask_id}")
    chosen = (row.get("chosen") or {}).get("chosen") or []
    ft = facts_mod.facts(conn, psg_db_path, chosen, project=row["project"])
    absences = absence_mod.absences(conn, ft)
    # the stored scope carries the ASK's wall time; reprinting it on a later
    # answer would attribute one read's cost to another, so it is dropped here
    # and whoever renders next measures its own.
    sc = {k: v for k, v in (row.get("scope") or scope_mod.scope(ft)).items() if k != "elapsed_ms"}
    return {"row": row, "facts": ft, "facts_text": facts_mod.render(ft), "facts_sha": facts_mod.sha(ft),
            "absences": absences, "scope": sc, "scope_line": scope_mod.line(sc, lang)}


def latest_answer(conn, ask_id: int) -> dict | None:
    """The newest draft of one question. Older versions are never overwritten."""
    row = conn.execute("SELECT id, ask_id, version, answer, cites_json, dropped_json, model, at "
                       "FROM ask_answer WHERE ask_id = ? ORDER BY version DESC LIMIT 1", (ask_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for k in ("cites_json", "dropped_json"):
        try:
            d[k[:-5]] = json.loads(d[k]) if d.get(k) else None
        except ValueError:
            d[k[:-5]] = None
    return d


def submit(conn, ask_id: int, draft: str, *, psg_db_path: str | None = None, model: str = "session",
           lang: str = "en", commit: bool = True) -> dict:
    """Check a session model's draft and append it as the next version."""
    import time

    from . import scope as scope_mod, summarize as summarize_mod

    started = time.perf_counter()
    base = rebuild(conn, ask_id, psg_db_path=psg_db_path, lang=lang)
    checked = summarize_mod.review(draft, base["facts"], absences=base["absences"],
                                   scope_line=base["scope_line"], lang=lang)
    version = int(conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM ask_answer WHERE ask_id = ?",
                               (ask_id,)).fetchone()[0])
    conn.execute("INSERT INTO ask_answer (ask_id, version, answer, cites_json, dropped_json, model) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (ask_id, version, checked["answer"],
                  json.dumps(checked["cites"], ensure_ascii=False),
                  json.dumps(checked["dropped"], ensure_ascii=False), model))
    if commit:
        conn.commit()
    logged_sha = base["row"].get("facts_sha")
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    timed = dict(base["scope"], elapsed_ms=elapsed_ms)
    doc = {"ask_id": ask_id, "version": version, "model": model, "runner": "session",
           "facts_changed": bool(logged_sha and logged_sha != base["facts_sha"]),
           "facts_sha_asked": logged_sha,
           "project": base["row"]["project"], "question": base["row"]["question"],
           "facts": base["facts"], "facts_text": base["facts_text"], "facts_sha": base["facts_sha"],
           "absences": base["absences"], "scope": base["scope"], "scope_line": base["scope_line"],
           "scope_line_timed": scope_mod.line(timed, lang), "elapsed_ms": elapsed_ms,
           "degraded": False, "degraded_reason": None, "lang": lang, "draft": draft, **checked}
    doc["records"] = records(base["facts"], checked["cites"])
    return doc


NEXT_COMMANDS = ("[Open records] provledger why {node}",
                 "[Export] provledger ask card {ask_id} --out card.md")


def next_commands(doc: dict) -> list[str]:
    """The two reads that follow an answer — never a write, never a button."""
    nodes = [n["qn"] for n in doc["facts"]["nodes"]] or ["<node>"]
    return [NEXT_COMMANDS[0].format(node=nodes[0]),
            NEXT_COMMANDS[1].format(ask_id=doc["ask_id"])]
