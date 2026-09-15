"""reasons — close-time, closed-form capture of WHY a plan changed each data point.

Spec §2.8 + decision provenance phase 1. When a registered-project plan
closes, the nodes its runs changed (psg_bridge.changed_node_keys) become a
closed checklist. The review agent answers each slot with exactly one of
three shapes — a span of the user's recorded words (stated), an
interpretation with optional references (asserted), or an explicit
`unstated` — and the tier follows the shape, never the sender (A2/A4: the
old free-text shape is refused). Whatever is left unanswered at close is
backstopped as an `unstated` change_reason by the system so the gap is
visible (state `unknown` when the project closes in pending mode). Deviation
justifications and failure reasons become `rejected_path` rows anchored to
the changed node their step talked about. Nothing here ever blocks a close.
"""
from __future__ import annotations

import re

from . import db, provenance, psg_bridge

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
    """Nodes of the plan that already carry a change_reason row (any tier, incl. a rule's derived reason)."""
    return {r["node_key"] for r in conn.execute(
        "SELECT node_key FROM change_reason WHERE plan_id = ? AND role = 'reason' AND node_key IS NOT NULL", (plan_id,))}


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


# ── DP phase 1 (Task 4): three kinds of answer, one tier each ─────────────────

class FillInputError(ValueError):
    """The answer shape is wrong (exit 2 in reason-fill.sh): nothing is written."""


REJECTED_TEXT_SHAPE = '"text" is not accepted: pass interpretation (asserted) or utterance_id+span (stated)'


def _answer_kind(item: dict) -> str:
    """stated | asserted | unstated — or FillInputError for the old {text} shape / a mixed item."""
    if not isinstance(item, dict) or not item.get("node_key"):
        raise FillInputError("every answer needs a node_key")
    if "text" in item:
        raise FillInputError(REJECTED_TEXT_SHAPE)
    has_span = "utterance_id" in item or "span" in item
    has_interp = "interpretation" in item
    has_unstated = bool(item.get("unstated"))
    if sum((has_span, has_interp, has_unstated)) != 1:
        raise FillInputError("an answer is exactly one of {utterance_id, span} (stated) | {interpretation, refs?} (asserted) | {unstated: true}")
    if has_span:
        span = item.get("span")
        if item.get("utterance_id") is None or not (isinstance(span, (list, tuple)) and len(span) == 2):
            raise FillInputError("a stated answer is {node_key, utterance_id, span: [start, end]}")
        return "stated"
    if has_interp:
        if not isinstance(item["interpretation"], str) or not item["interpretation"].strip():
            raise FillInputError("interpretation must be a non-empty string")
        return "asserted"
    return "unstated"


def fill(conn, *, project: str, plan_id: str, run_id: int | None, reasons: list[dict],
         source: str = "agent", step_id: str | None = None, psg_db_path: str | None = None) -> dict:
    """Record the answers into change_reason. Each item is exactly one of
      {node_key, utterance_id, span:[s,e]}        -> stated   (a span of the user's recorded words)
      {node_key, interpretation, refs?:[ref_id]}  -> asserted (the agent's or a person's reading)
      {node_key, unstated: true}                  -> unstated (an explicit gap)
    The old {node_key, text} shape is refused (FillInputError, exit 2) — free
    text can no longer become stated, whoever sends it (A2/A4). Keys outside
    the plan's change set are refused all-or-nothing; the shapes are checked
    before anything is written."""
    kinds = [_answer_kind(r) for r in reasons]                     # shape errors first, nothing written
    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    changed = {c["node_key"] for c in psg_bridge.changed_node_keys(psg, plan_id)}
    unknown = [r.get("node_key") for r in reasons if r.get("node_key") not in changed]
    if unknown:
        return {"filled": 0, "stated": 0, "asserted": 0, "unstated": 0, "unknown_keys": unknown}
    recorded_by = source if source in provenance.RECORDED_BY else "agent"
    counts = {"stated": 0, "asserted": 0, "unstated": 0}
    with db.transaction(conn):
        for r, kind in zip(reasons, kinds):
            common = dict(project=project, plan_id=plan_id, node_key=r["node_key"], kind=r.get("kind", "technical"),
                          run_id=run_id, step_id=step_id, recorded_by=recorded_by, commit=False)
            if kind == "stated":
                s, e = r["span"]
                provenance.insert_reason(conn, verbatim=(int(r["utterance_id"]), int(s), int(e)), **common)
            elif kind == "asserted":
                provenance.insert_reason(conn, interpretation=r["interpretation"].strip(),
                                         refs=[int(x) for x in (r.get("refs") or [])], **common)
            else:
                provenance.insert_reason(conn, **common)
            counts[kind] += 1
    return {"filled": counts["stated"] + counts["asserted"], **counts, "unknown_keys": []}


PREVIEW_CHARS = 120


def draft(conn, project: str, plan_id: str, psg_db_path: str | None, per_slot: int = 2) -> list[dict]:
    """For every open slot, the candidate utterances (plan window + session): an
    R0 literal hit scores 10 and proposes that sentence as the span; plain
    token overlap proposes the whole utterance. Deterministic, top `per_slot`;
    each candidate is ready to be sent back as a stated answer."""
    from . import triggers
    ctx = triggers._ctx(conn, project, plan_id, psg_db_path)
    utts = triggers.candidate_utterances(ctx)
    out = []
    for slot in slots_for_plan(conn, project, plan_id, psg_db_path):
        qn = slot.get("qualified_name") or ""
        node = ctx.touched.get(slot["node_key"]) or {"node_key": slot["node_key"], "qualified_name": qn, "file_path": None}
        local = qn.split(".")[-1].split(":")[-1]
        keys = _tokens(local, local.replace("_", " "), qn.split(".")[-2] if "." in qn else "")
        pats = [triggers._literal(n) for n in triggers.r0_names(node)]
        cands = []
        for u in utts:
            # R0 first: a sentence that names the node literally is the answer, scored above any token overlap
            r0 = None
            for s, e in triggers.sentences(u["text"]):
                if any(p.search(u["text"][s:e]) for p in pats):
                    r0 = (s, e)
                    break
            score = len(keys & _tokens(u["text"])) + (10 if r0 else 0)
            if score:
                span = list(r0) if r0 else [0, len(u["text"])]
                cands.append({"utterance_id": u["id"], "span": span, "score": score, "kind": "r0" if r0 else "overlap",
                              "preview": u["text"][span[0]:span[0] + PREVIEW_CHARS]})
        cands.sort(key=lambda c: (-c["score"], c["utterance_id"]))
        out.append({"node_key": slot["node_key"], "qualified_name": qn, "candidates": cands[:per_slot]})
    return out


# ── close-time backstops ─────────────────────────────────────────────────────

def backstop_unstated(conn, *, project: str, plan_id: str, psg_db_path: str | None,
                      commit: bool = False, state: str = "active") -> int:
    """Every changed node without any reason row gets an explicit `unstated`
    change_reason from the system — the gap is recorded, never papered over.
    `state="unknown"` is the pending close mode (nobody was asked yet)."""
    have = _reason_keys(conn, plan_id)
    n = 0
    for c in psg_bridge.changed_node_keys(psg_db_path, plan_id):
        if c["node_key"] in have:
            continue
        provenance.insert_reason(conn, project=project, plan_id=plan_id, node_key=c["node_key"], kind="technical",
                                 run_id=c["run_id"], recorded_by="system", state=state, commit=False)
        n += 1
    if commit:
        conn.commit()
    return n


def rejected_paths(conn, *, project: str, plan_id: str, psg_db_path: str | None,
                   commit: bool = False) -> int:
    """Deviation justifications and failed steps become rejected_path rows —
    since DP phase 1 through rule R6 (tier derived, rule_id R6, the failed
    command + the error tail), anchored to the changed node they name."""
    from . import triggers
    return triggers.rejected_paths(conn, project=project, plan_id=plan_id, psg_db_path=psg_db_path, commit=commit)


def unstated_ratio(conn, project: str, plan_id: str) -> dict:
    """{slots, unstated, ratio}: a slot is answered when ANY of its change_reason
    rows has a tier other than unstated (the legacy rows were migrated in)."""
    stated: dict[str, bool] = {}
    for r in conn.execute("SELECT node_key, tier FROM change_reason WHERE plan_id = ? AND role = 'reason' AND node_key IS NOT NULL", (plan_id,)):
        stated[r["node_key"]] = stated.get(r["node_key"], False) or (r["tier"] != "unstated")
    slots = len(stated)
    unstated = sum(1 for v in stated.values() if not v)
    return {"slots": slots, "unstated": unstated, "ratio": round(unstated / slots, 4) if slots else 0.0}


def auto_filled(conn, plan_id: str) -> list[dict]:
    """Reasons a rule filled for the plan (rule_id + basis) — shown in the checklist."""
    from . import triggers
    return triggers.auto_filled(conn, plan_id)
