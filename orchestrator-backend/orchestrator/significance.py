"""significance — a computed hint on every reason, an optional logged LLM
verdict, a human override (DP phase 2b, Task 2; spec §17, I14).

The computation layer never changes: node_event records everything. The
threshold only shapes how reasons are SHOWN and ASKED about. For each reason
the system first computes a hint (derived) — any of six signals → major:
the node's struct_sig changed in the plan's run, it has ≥ 1 downstream
consumer, the plan logged a gate failure, an active constraint anchors on
the node, an outcome was recorded for it, or the reason is the user's own
words (tier stated). Otherwise minor. An LLM may overrule the hint, but only
through `judge()`, which writes the verdict next to the hint in
significance_log — believed, but on the record. `mark()` is a person's word
(judged_by human). Nothing here ever runs a model unless the extensions say
`reasons.significance: llm`, and never in CI.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import psg_bridge

LEVELS = ("major", "minor")
PROMPT_PATH = Path(__file__).resolve().parent / "testing" / "prompts" / "significance.md"
_JSON_RE = re.compile(r"\{.*\}", re.S)


# ── the hint ────────────────────────────────────────────────────────────────

def _struct_changed(psg, node_key: str | None, run_id: int | None) -> bool:
    if not (psg and node_key and run_id):
        return False
    for r in psg_bridge._query(psg, "SELECT payload_json FROM node_event WHERE node_key = ? AND run_id = ? AND event_type IN ('node_changed', 'node_added')", (node_key, run_id)):
        try:
            payload = json.loads(r[0] or "{}")
        except ValueError:
            payload = {}
        if "struct_sig" in (payload.get("changed") or []) or not payload.get("changed"):
            return True
    return False


def _consumers(psg, qn: str | None) -> int:
    if not (psg and qn):
        return 0
    card = psg_bridge.card_of(psg, qn)
    return len(card.get("output_consumers") or []) + len(card.get("lineage_downstream") or [])


def _gate_failed(conn, plan_id: str) -> bool:
    for (log,) in conn.execute("SELECT log_context FROM Steps WHERE plan_id = ? AND COALESCE(is_review, 0) = 1", (plan_id,)):
        for line in (log or "").splitlines():
            low = line.lower()
            if "[fail]" in low or "gates failed" in low or "verdict: fail" in low:
                return True
    return False


def _active_constraint(conn, project: str, node_key: str | None, qn: str | None) -> bool:
    keys = [k for k in (node_key, qn) if k]
    if not keys:
        return False
    ph = ",".join("?" * len(keys))
    return conn.execute(f"SELECT 1 FROM change_reason WHERE project = ? AND role = 'constraint' AND state = 'active' AND superseded_by IS NULL AND node_key IN ({ph}) LIMIT 1",
                        (project, *keys)).fetchone() is not None


def _has_outcome(conn, project: str, qn: str | None, plan_id: str) -> bool:
    if not qn:
        return False
    return conn.execute("SELECT 1 FROM outcomes o JOIN expectations e ON e.id = o.expectation_id WHERE e.project = ? AND (e.target = ? OR e.plan_id = ?) LIMIT 1",
                        (project, qn, plan_id)).fetchone() is not None


def hint(conn, reason: dict, psg_db_path: str | None = None) -> tuple[str, str]:
    """('major' | 'minor', basis). `reason` is a change_reason(_v) row as a dict."""
    project, plan_id = reason["project"], reason["plan_id"]
    key = reason.get("node_key")
    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    qn = psg_bridge.latest_qualified_name(psg, key) if (psg and key) else None
    signals = []
    if reason.get("tier") == "stated":
        signals.append("the user's own words")
    if _struct_changed(psg, key, reason.get("run_id")):
        signals.append("struct_sig changed")
    n = _consumers(psg, qn)
    if n:
        signals.append(f"{n} downstream consumer(s)")
    if _gate_failed(conn, plan_id):
        signals.append("a gate failed in this plan")
    if _active_constraint(conn, project, key, qn):
        signals.append("an active constraint anchors here")
    if _has_outcome(conn, project, qn, plan_id):
        signals.append("an outcome is recorded")
    if signals:
        return "major", "; ".join(signals)
    return "minor", "none of: stated words / struct_sig change / downstream consumers / gate failure / active constraint / outcome"


def log_hint(conn, reason: dict, level: str, basis: str, commit: bool = False) -> int:
    cur = conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, judged_by) VALUES (?, ?, ?, ?, 'hint')",
                       (reason["id"], reason["project"], level, basis))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


# ── the LLM verdict (optional, logged, strict JSON) ──────────────────────────

def prompt_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def prompt_for(reason: dict, qn: str | None, level: str, basis: str) -> str:
    payload = {"reason": reason.get("text") or reason.get("interpretation") or reason.get("statement") or "", "tier": reason.get("tier"),
               "node": qn or reason.get("node_key"), "role": reason.get("role"), "hint": level, "hint_basis": basis}
    return prompt_text().rstrip("\n") + "\n\n" + json.dumps(payload, ensure_ascii=False)


def parse_verdict(text: str | None) -> tuple[str, str] | None:
    """Strict: a JSON object with significance ∈ major|minor and a basis string; anything else → None."""
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        doc = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("significance") not in LEVELS or not isinstance(doc.get("basis"), str):
        return None
    return doc["significance"], doc["basis"].strip()[:400]


def judge(conn, reason: dict, *, runner=None, model: str | None = None, psg_db_path: str | None = None, commit: bool = False) -> dict:
    """Compute the hint, ask the runner, and log ONE row: hint + verdict when the
    answer is strict JSON, hint only (judged_by hint) when it is not. Never raises
    on a bad answer — the hint stands and the log says so."""
    level, basis = hint(conn, reason, psg_db_path)
    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(reason["project"])
    qn = psg_bridge.latest_qualified_name(psg, reason.get("node_key")) if (psg and reason.get("node_key")) else None
    if runner is None:
        from .testing.claude_arbiter import default_runner
        runner = default_runner
    raw = runner(prompt_for(reason, qn, level, basis), model=model)
    parsed = parse_verdict(raw)
    name = getattr(runner, "__name__", "runner")
    if parsed is None:
        rid = conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, judged_by, runner) VALUES (?, ?, ?, ?, 'hint', ?)",
                           (reason["id"], reason["project"], level, basis + " · llm answer was not strict JSON, hint stands", name)).lastrowid
        out = {"reason_id": reason["id"], "hint": level, "verdict": None, "log_id": int(rid)}
    else:
        verdict, vbasis = parsed
        rid = conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, verdict, verdict_basis, judged_by, runner) VALUES (?, ?, ?, ?, ?, ?, 'llm', ?)",
                           (reason["id"], reason["project"], level, basis, verdict, vbasis, name)).lastrowid
        out = {"reason_id": reason["id"], "hint": level, "verdict": verdict, "log_id": int(rid)}
    if commit:
        conn.commit()
    return out


# ── close-time application and the human mark ───────────────────────────────

def _reasons_of_plan(conn, plan_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT r.id, r.project, r.plan_id, r.node_key, r.run_id, r.role, r.tier, "
        "       COALESCE(r.interpretation, r.statement, substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start)) AS text "
        "FROM change_reason_v r LEFT JOIN utterance u ON u.id = r.verbatim_utterance_id "
        "WHERE r.plan_id = ? AND r.role = 'reason' AND r.tier <> 'unstated' "
        "AND NOT EXISTS (SELECT 1 FROM significance_log s WHERE s.reason_id = r.id) ORDER BY r.id", (plan_id,))]


def apply_for_plan(conn, *, project: str, plan_id: str, psg_db_path: str | None, mode: str = "hint", runner=None, commit: bool = False) -> dict:
    """At close: every new reason of the plan gets exactly one hint row; with
    mode='llm' a verdict is asked for as well. mode='hint' calls no runner."""
    rows = _reasons_of_plan(conn, plan_id)
    out = {"plan_id": plan_id, "mode": mode, "judged": 0, "major": 0, "minor": 0, "verdicts": 0}
    for r in rows:
        if mode == "llm":
            j = judge(conn, r, runner=runner, psg_db_path=psg_db_path, commit=False)
            level = j["verdict"] or j["hint"]
            out["verdicts"] += 1 if j["verdict"] else 0
        else:
            level, basis = hint(conn, r, psg_db_path)
            log_hint(conn, r, level, basis, commit=False)
        out["judged"] += 1
        out[level] += 1
    if commit:
        conn.commit()
    return out


def mark(conn, reason_id: int, level: str, *, by: str = "human", basis: str | None = None, psg_db_path: str | None = None, commit: bool = True) -> int:
    """A person's word: a human row with the verdict; the hint is recomputed so the row is complete."""
    if level not in LEVELS:
        raise ValueError("level is major | minor")
    if by != "human":
        raise ValueError("mark is a person's word — by must be human")
    row = conn.execute("SELECT id, project, plan_id, node_key, run_id, role, tier FROM change_reason WHERE id = ?", (reason_id,)).fetchone()
    if row is None:
        raise ValueError(f"reason {reason_id} does not exist")
    reason = dict(zip(("id", "project", "plan_id", "node_key", "run_id", "role", "tier"), row))
    h, hb = hint(conn, reason, psg_db_path)
    cur = conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, verdict, verdict_basis, judged_by) VALUES (?, ?, ?, ?, ?, ?, 'human')",
                       (reason_id, reason["project"], h, hb, level, basis or "marked by a person"))
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def disagreements(conn, project: str | None = None) -> list[dict]:
    """hint = major but the latest verdict says minor — the list selfcheck and the eval print."""
    sql = ("SELECT s.reason_id, s.project, s.hint, s.hint_basis, s.verdict, s.verdict_basis, s.judged_by, s.runner, s.at FROM significance_log s "
           "WHERE s.verdict = 'minor' AND s.hint = 'major' AND s.id = (SELECT MAX(id) FROM significance_log x WHERE x.reason_id = s.reason_id AND x.verdict IS NOT NULL)")
    args: tuple = ()
    if project:
        sql += " AND s.project = ?"; args = (project,)
    return [dict(zip(("reason_id", "project", "hint", "hint_basis", "verdict", "verdict_basis", "judged_by", "runner", "at"), r)) for r in conn.execute(sql, args)]


def confusion(conn, project: str | None = None) -> dict:
    """hint × verdict counts over rows that carry a verdict."""
    sql = "SELECT hint, verdict, COUNT(*) FROM significance_log WHERE verdict IS NOT NULL"
    args: tuple = ()
    if project:
        sql += " AND project = ?"; args = (project,)
    sql += " GROUP BY 1, 2"
    out = {f"{h}->{v}": 0 for h in LEVELS for v in LEVELS}
    for h, v, n in conn.execute(sql, args):
        out[f"{h}->{v}"] = n
    return out
