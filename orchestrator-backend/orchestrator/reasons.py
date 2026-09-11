"""reasons — close-time, closed-form capture of WHY a plan changed each data point.

Spec §2.8. When a registered-project plan closes, the nodes its runs changed
(psg_bridge.changed_node_keys) become a closed checklist: the review agent
answers one sentence per node (reason-fill), "unstated" is stored as NULL —
explicitly unknown, never fabricated — and whatever is left unanswered at
close is backstopped as NULL by the system so the gap is visible. Deviation
justifications and failure reasons become `rejected_path` rows anchored to
the changed node their step talked about. Nothing here ever blocks a close.
"""
from __future__ import annotations

import re

from . import db, psg_bridge

# Same tokenizer/stopword rule as writing-plans' impact_preflight (the backend
# must not import skills, so the 8 lines are duplicated on purpose).
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "but", "not",
    "you", "are", "was", "all", "any", "can", "has", "have", "will", "please",
    "function", "method", "step", "code", "change", "update", "add", "fix",
    "refactor", "touch",
}
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
UNSTATED_WORDS = {"", "unstated", "unknown", "n/a", "none", "?"}


def _tokens(*texts: str | None) -> set[str]:
    out: set[str] = set()
    for t in texts:
        for tok in _TOKEN_RE.findall(t or ""):
            low = tok.lower()
            if low not in _STOPWORDS:
                out.add(low)
    return out


def _normalize_text(text) -> str | None:
    if text is None:
        return None
    t = str(text).strip()
    return None if t.lower() in UNSTATED_WORDS else t


def _reason_keys(conn, plan_id: str) -> set[str]:
    return {r["node_key"] for r in db.get_node_reasons(conn, plan_id=plan_id)
            if r["kind"] == "reason" and r["node_key"]}


# ── the checklist ────────────────────────────────────────────────────────────

def slots_for_plan(conn, project: str, plan_id: str, psg_db_path: str | None) -> list[dict]:
    """Changed nodes of the plan that have no `reason` row yet."""
    have = _reason_keys(conn, plan_id)
    return [c for c in psg_bridge.changed_node_keys(psg_db_path, plan_id) if c["node_key"] not in have]


def checklist_text(slots: list[dict]) -> str:
    """Closed-form: exactly these N data points, numbered, nothing else to invent."""
    if not slots:
        return "本次改动没有触及任何数据点（无 node_changed / node_added / node_removed），不需要说明原因。"
    lines = [f"本次改动触及 {len(slots)} 个数据点，逐个用一句话说明原因；不知道就写 unstated："]
    for i, s in enumerate(slots, 1):
        lines.append(f"  {i}. {s['qualified_name']} ({s['node_type']}, {'/'.join(s['event_types'])}) [{s['node_key']}]")
    return "\n".join(lines)


def fill(conn, *, project: str, plan_id: str, run_id: int | None, reasons: list[dict],
         source: str = "agent", step_id: str | None = None, psg_db_path: str | None = None) -> dict:
    """Record the answers. Keys outside the plan's change set are refused
    (all-or-nothing, nothing written); "unstated"/empty becomes NULL."""
    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    changed = {c["node_key"] for c in psg_bridge.changed_node_keys(psg, plan_id)}
    unknown = [r.get("node_key") for r in reasons if r.get("node_key") not in changed]
    if unknown:
        return {"filled": 0, "unstated": 0, "unknown_keys": unknown}
    filled = unstated = 0
    with db.transaction(conn):
        for r in reasons:
            text = _normalize_text(r.get("text"))
            db.insert_node_reason(conn, node_key=r["node_key"], project=project, run_id=run_id, plan_id=plan_id,
                                  step_id=step_id, kind="reason", text=text, source=source, tier="stated",
                                  commit=False)
            if text is None:
                unstated += 1
            else:
                filled += 1
    return {"filled": filled, "unstated": unstated, "unknown_keys": []}


# ── close-time backstops ─────────────────────────────────────────────────────

def backstop_unstated(conn, *, project: str, plan_id: str, psg_db_path: str | None,
                      commit: bool = False) -> int:
    """Every changed node without any reason row gets an explicit NULL from
    the system (tier derived) — the gap is recorded, never papered over."""
    have = _reason_keys(conn, plan_id)
    n = 0
    for c in psg_bridge.changed_node_keys(psg_db_path, plan_id):
        if c["node_key"] in have:
            continue
        db.insert_node_reason(conn, node_key=c["node_key"], project=project, run_id=c["run_id"], plan_id=plan_id,
                              kind="reason", text=None, source="system", tier="derived", commit=commit)
        n += 1
    return n


def _anchor(text: str | None, changed: list[dict]) -> str | None:
    """The changed node whose local name appears in `text` (first match in
    change-set order); None when the text names none of them."""
    toks = _tokens(text)
    for c in changed:
        tail = (c.get("qualified_name") or "").split(".")[-1].split(":")[-1].split("#")[0].lower()
        if tail and tail in toks:
            return c["node_key"]
    return None


def rejected_paths(conn, *, project: str, plan_id: str, psg_db_path: str | None,
                   commit: bool = False) -> int:
    """Deviation justifications and unrecovered failure reasons of the plan
    become rejected_path rows (tier asserted — the anchor is inferred from the
    step description / justification, not stated by the author). Idempotent
    per (plan, text)."""
    changed = psg_bridge.changed_node_keys(psg_db_path, plan_id)
    existing = {r["text"] for r in db.get_node_reasons(conn, plan_id=plan_id) if r["kind"] == "rejected_path"}
    run_id = max((c["run_id"] for c in changed), default=None)
    n = 0
    candidates: list[tuple[str, str | None, str]] = []      # (text, step_id, anchor_text)
    for d in db.get_deviations(conn, plan_id):
        step = db.get_step(conn, d["target_step_id"]) if d.get("target_step_id") else None
        anchor_text = " ".join(filter(None, [step.get("description") if step else "", d.get("justification")]))
        candidates.append((d["justification"], d.get("target_step_id"), anchor_text))
    for s in db.get_steps(conn, plan_id):
        if s["status"] == "FAILED" and s.get("failure_reason") and not s.get("is_review"):
            candidates.append((s["failure_reason"], s["step_id"],
                               " ".join(filter(None, [s.get("description"), s["failure_reason"]]))))
    for text, step_id, anchor_text in candidates:
        if not text or text in existing:
            continue
        db.insert_node_reason(conn, node_key=_anchor(anchor_text, changed), project=project, run_id=run_id,
                              plan_id=plan_id, step_id=step_id, kind="rejected_path", text=text,
                              source="agent", tier="asserted", commit=commit)
        existing.add(text)
        n += 1
    return n


def unstated_ratio(conn, project: str, plan_id: str) -> dict:
    """{slots, unstated, ratio}: a slot is stated when ANY of its reason rows carries text."""
    stated: dict[str, bool] = {}
    for r in db.get_node_reasons(conn, plan_id=plan_id):
        if r["kind"] != "reason" or not r["node_key"]:
            continue
        stated[r["node_key"]] = stated.get(r["node_key"], False) or (r["text"] is not None)
    slots = len(stated)
    unstated = sum(1 for v in stated.values() if not v)
    return {"slots": slots, "unstated": unstated, "ratio": round(unstated / slots, 4) if slots else 0.0}
