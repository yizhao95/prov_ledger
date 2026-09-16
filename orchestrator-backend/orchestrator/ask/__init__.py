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
        if d.get(k):
            try:
                d[k[:-5]] = json.loads(d[k])
            except ValueError:
                d[k[:-5]] = None
    return d
