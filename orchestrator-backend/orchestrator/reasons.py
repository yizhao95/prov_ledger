"""reasons — close-time capture of WHY a plan was done, once per task.

Spec §2.8 + decision provenance, reshaped by the task-level redesign (step 3a):
a plan is a task, and its close records why the task was done — the root, what
was seen along the way, what was not chosen — with exactly one of three shapes:
a span of the user's recorded words (stated), an interpretation with optional
references (asserted), or an explicit `unstated`. The tier follows the shape,
never the sender. When a plan did several things, each gets its own reason, the
changed nodes are assigned to one of them, and one sentence says why they share
a plan. The nodes reach their task's reason through the state graph's history
(covering_task_reasons); nothing is copied onto them. A plan nobody explained is
backstopped with one `unstated` task reason by the system. Failed steps and
deviations become `rejected_path` rows (rule R6). Nothing here ever blocks a
close.
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
        return ("This change touched no data points at all (no node_changed / node_added / "
                "node_removed), so there is nothing to explain.")
    lines = [f"This change touched {len(slots)} data point(s). Give one sentence for each; "
             "write unstated when you do not know:"]
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
    because = item.get("because")
    if because is not None and not (isinstance(because, (list, tuple)) and all(isinstance(x, int) for x in because)):
        raise FillInputError("because must be a list of reason ids")
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


PER_NODE_SHAPE = ("close-time reasons are per task now: send tasks: [{interpretation | utterance_id+span | unstated, "
                  "nodes?, refs?, because?}] (and a bundle sentence when there are several), not one item per node_key")


def fill(conn, *, project: str, plan_id: str, run_id: int | None, reasons: list[dict],
         source: str = "agent", step_id: str | None = None, psg_db_path: str | None = None) -> dict:
    """The per-node close (one reason per changed node) is gone (task-level
    redesign, step 3a): a plan's reasons are per task, see fill_tasks."""
    raise FillInputError(PER_NODE_SHAPE)


# ── task-level redesign, step 3a: one reason per task ─────────────────────────

def _task_kind(item) -> str:
    """stated | asserted | unstated for one task, or FillInputError."""
    if not isinstance(item, dict):
        raise FillInputError("a task is an object: {interpretation | utterance_id+span | unstated, nodes?, refs?, because?}")
    if "node_key" in item:
        raise FillInputError(PER_NODE_SHAPE)
    nodes = item.get("nodes")
    if nodes is not None and not (isinstance(nodes, list) and all(isinstance(n, str) and n for n in nodes)):
        raise FillInputError("a task's nodes are a list of node keys or qualified names")
    return _answer_kind({**item, "node_key": "task"})


def _changed(psg, plan_id: str) -> list[dict]:
    return psg_bridge.changed_node_keys(psg, plan_id) if psg else []


def fill_tasks(conn, *, project: str, plan_id: str, run_id: int | None, tasks: list[dict], bundle: str | None = None,
               source: str = "agent", step_id: str | None = None, psg_db_path: str | None = None) -> dict:
    """Record why the plan was done, once per task. Each task is exactly one of
      {utterance_id, span:[s,e]}        -> stated   (a span of the user's recorded words)
      {interpretation, refs?:[ref_id]}  -> asserted (a reading: the root, what was seen, what was not chosen)
      {unstated: true}                  -> unstated (nobody said)
    plus because: [reason_id, ...] (influence, via reason_because). One task
    covers every node the plan changed. Several tasks name their nodes — node
    keys or qualified names of the plan's changed nodes, each in one task — all
    but one, which takes the rest; a `bundle` sentence then says why they share
    the plan. Everything is checked before anything is written; a node the plan
    did not change comes back in unknown_nodes and nothing is written."""
    if not isinstance(tasks, list) or not tasks:
        raise FillInputError("a close needs at least one task")
    kinds = [_task_kind(t) for t in tasks]
    several = len(tasks) > 1
    if several:
        if not (isinstance(bundle, str) and bundle.strip()):
            raise FillInputError("several tasks in one plan need a bundle sentence: why they share the plan")
        if sum(1 for t in tasks if not t.get("nodes")) > 1:
            raise FillInputError("with several tasks, every task but one names its nodes; the one that does not takes the rest")
    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    changed = _changed(psg, plan_id)
    by_name = {c["node_key"]: c["node_key"] for c in changed}
    by_name.update({c["qualified_name"]: c["node_key"] for c in changed if c.get("qualified_name")})
    unknown, seen, assigned = [], {}, []
    for i, t in enumerate(tasks):
        keys = []
        for n in (t.get("nodes") or []) if several else []:
            k = by_name.get(n)
            if k is None:
                unknown.append(n)
                continue
            if k in seen and seen[k] != i:
                raise FillInputError(f"{k} is named by two tasks; a node belongs to one")
            seen[k] = i
            keys.append(k)
        assigned.append(keys)
    out = {"tasks": 0, "stated": 0, "asserted": 0, "unstated": 0, "adopted": 0, "unknown_nodes": unknown, "reason_ids": []}
    if unknown:
        return out
    recorded_by = source if source in provenance.RECORDED_BY else "agent"
    by = "human" if recorded_by == "human" else "agent"
    with db.transaction(conn):
        for t, kind, keys in zip(tasks, kinds, assigned):
            common = dict(project=project, plan_id=plan_id, node_key=None, kind=t.get("kind", "technical"),
                          run_id=run_id, step_id=step_id, recorded_by=recorded_by, commit=False)
            try:
                if kind == "stated":
                    s, e = t["span"]
                    rid = provenance.insert_reason(conn, verbatim=(int(t["utterance_id"]), int(s), int(e)), **common)
                elif kind == "asserted":
                    rid = provenance.insert_reason(conn, interpretation=t["interpretation"].strip(),
                                                   refs=[int(x) for x in (t.get("refs") or [])], **common)
                else:
                    rid = provenance.insert_reason(conn, **common)
            except ValueError as e:                 # a span outside its utterance, injected text, a bad ref
                raise FillInputError(str(e)) from None
            out["reason_ids"].append(rid)
            out[kind] += 1
            out["tasks"] += 1
            for k in keys:
                conn.execute("INSERT INTO reason_node (reason_id, node_key) VALUES (?, ?)", (rid, k))
            for cited in t.get("because") or ():
                if provenance.get_reason(conn, int(cited)) is None:
                    raise FillInputError(f"because: reason {cited} does not exist")
                conn.execute("INSERT INTO influence (reason_id, project, plan_id, step_id, node_key, via, by) "
                             "VALUES (?, ?, ?, ?, NULL, 'reason_because', ?)", (int(cited), project, plan_id, step_id, by))
                out["adopted"] += 1
        if several:
            conn.execute("INSERT INTO plan_bundle (plan_id, basis, recorded_by) VALUES (?, ?, ?)",
                         (plan_id, bundle.strip(), recorded_by))
    return out


def per_node_close_record(row: dict) -> bool:
    """A reason the close wrote about one node, before task reasons (0.4.8):
    rules R0-R5, the agent's per-node answers, the per-node unstated backstop,
    and the corrections recheck wrote of them. A person's note on a node and the
    external trigger's row are not that — they still name one node on purpose."""
    from . import external_trigger
    return (row.get("role") == "reason" and row.get("node_key") is not None
            and row.get("recorded_by") in ("agent", "system") and row.get("rule_id") != external_trigger.RULE_ID)


def _live_task_reasons(conn, plan_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM change_reason WHERE plan_id = ? AND role = 'reason' AND node_key IS NULL "
        "AND superseded_by IS NULL ORDER BY id", (plan_id,))]


def covering_task_reasons(conn, node_key: str, plan_ids) -> list[int]:
    """The live task reasons, among these plans, that answer for this node: the
    task the node was assigned to, else the plan's task that names no nodes.
    A reason that corrects an assigned one inherits its assignment."""
    out: list[int] = []
    for pid in dict.fromkeys(plan_ids):
        live = _live_task_reasons(conn, pid)
        if not live:
            continue
        def nodes_of(rid):
            ids = [rid] + [r[0] for r in conn.execute("SELECT id FROM change_reason WHERE superseded_by = ?", (rid,))]
            return {r[0] for r in conn.execute(
                f"SELECT node_key FROM reason_node WHERE reason_id IN ({','.join('?' * len(ids))})", ids)}
        assigned = {r["id"]: nodes_of(r["id"]) for r in live}
        mine = [rid for rid, ns in assigned.items() if node_key in ns]
        out += mine or [rid for rid, ns in assigned.items() if not ns]
    return out


def task_nodes(conn, psg, reason_id: int) -> list[str]:
    """The nodes a reason answers for — the inverse of covering_task_reasons. A
    node's reason answers for its node; a task reason for the nodes its plan
    changed that it covers (reason_node, or all of them for a plan's only task);
    a reason a later one corrected, for what the correction covers."""
    row = conn.execute("SELECT id, plan_id, node_key, role, superseded_by FROM change_reason WHERE id = ?",
                       (reason_id,)).fetchone()
    seen: set[int] = set()
    while row is not None and row[4] is not None and row[0] not in seen:
        seen.add(row[0])
        row = conn.execute("SELECT id, plan_id, node_key, role, superseded_by FROM change_reason WHERE id = ?",
                           (row[4],)).fetchone()
    if row is None:
        return []
    if row[2] is not None:
        return [row[2]]
    if row[3] != "reason" or not row[1]:
        return []
    keys = [c["node_key"] for c in psg_bridge.changed_node_keys(psg, row[1]) if c.get("node_key")]
    return [k for k in keys if row[0] in covering_task_reasons(conn, k, [row[1]])]


def task_reasons_for(conn, psg, node_keys) -> dict[int, str]:
    """{reason_id: plan_id}: the live task reasons that answer for any of these
    nodes, through the plans whose runs changed them (psg_bridge.plans_that_changed).
    The one path every reader takes; `psg` is a path or an open connection."""
    out: dict[int, str] = {}
    for key, plans in psg_bridge.plans_that_changed(psg, node_keys).items():
        for pid in plans:
            for rid in covering_task_reasons(conn, key, [pid]):
                out.setdefault(rid, pid)
    return out


def task_slots(conn, project: str, plan_id: str, psg_db_path: str | None, draft: bool = False) -> dict:
    """What the agent needs to write the plan's task reasons: the nodes it
    changed (to assign, when it did several things), its root and the root's
    words, the task reasons already written, and optionally draft sentences."""
    from . import roots
    root = roots.root_of(conn, plan_id)
    out = {"nodes": [dict(c) for c in _changed(psg_db_path, plan_id)],
           "root": {"root_plan_id": root, "words": roots._words(conn, root)} if root else None,
           "tasks": [{"reason_id": r["id"], "tier": r["tier"],
                      "text": r["interpretation"] or r["statement"],
                      "nodes": sorted(x[0] for x in conn.execute("SELECT node_key FROM reason_node WHERE reason_id = ?", (r["id"],)))}
                     for r in _live_task_reasons(conn, plan_id)]}
    if draft:
        out["draft"] = _draft_sentences(conn, project, plan_id, psg_db_path)      # `draft` here is the flag
    return out


PREVIEW_CHARS = 120


def draft(conn, project: str, plan_id: str, psg_db_path: str | None, limit: int = 3) -> list[dict]:
    """Candidate sentences for the plan's task reason, from the words recorded
    in the plan's window and session (never text Claude Code injected): a
    sentence that names a changed node scores 10, plus its token overlap with
    the plan's goal and the changed nodes' names. Deterministic; each is ready
    to send back as a stated task."""
    from . import triggers
    ctx = triggers._ctx(conn, project, plan_id, psg_db_path)
    plan = db.get_plan(conn, plan_id) or {}
    changed = [n for n in ctx.touched.values() if set(n.get("event_types") or []) - triggers.PASSIVE_EVENTS]
    keys = _tokens(plan.get("original_goal"), plan.get("user_query"))
    for n in changed:
        qn = n.get("qualified_name") or ""
        local = qn.split(".")[-1].split(":")[-1]
        keys |= _tokens(local, local.replace("_", " "), qn.split(".")[-2] if "." in qn else "")
    pats = [triggers._literal(x) for n in changed for x in triggers.r0_names(n)]
    cands = []
    for u in triggers.candidate_utterances(ctx):
        best = None
        for s, e in triggers.sentences(u["text"]):
            sent = u["text"][s:e]
            score = (10 if any(p.search(sent) for p in pats) else 0) + len(keys & _tokens(sent))
            if score and (best is None or score > best[0]):
                best = (score, s, e)
        if best:
            score, s, e = best
            cands.append({"utterance_id": u["id"], "span": [s, e], "score": score,
                          "kind": "names_a_node" if score >= 10 else "overlap",
                          "preview": u["text"][s:s + PREVIEW_CHARS].strip()})
    cands.sort(key=lambda c: (-c["score"], c["utterance_id"]))
    return cands[:limit]


_draft_sentences = draft


def task_text(slots: dict) -> str:
    """One log line for a task_slots() result: the changed nodes and the task
    reasons written so far (the [REASON SLOTS] line of a reopened close)."""
    names = [n.get("qualified_name") or n["node_key"] for n in slots["nodes"]]
    tasks = [f"#{t['reason_id']} {t['tier']}" + (f" ({len(t['nodes'])} node(s))" if t["nodes"] else "")
             for t in slots["tasks"]]
    return (f"{len(names)} changed node(s)" + (": " + ", ".join(names) if names else "")
            + f"; {len(tasks)} task reason(s)" + (": " + ", ".join(tasks) if tasks else " (the close records one unstated)"))



# ── recheck (FL-238, FL-239) ─────────────────────────────────────────────────

def recheck(conn, *, project: str | None = None, apply: bool = False) -> dict:
    """The rows the close-time rules wrote before FL-238 / FL-239 that are not
    true: a `stated` reason quoting text Claude Code injected (nobody said it),
    and an R6 rejected path hung on a node its text never names as code.
    Without `apply` it only lists them. With `apply`, one transaction appends a
    correction for each — an `unstated` system row for the same plan and node,
    or the same R6 text anchored by today's rule — that supersedes it. The
    corrected rows keep their words (`provledger record #id`); readers show the
    correction. Plans whose graph cannot be read are listed, never guessed at."""
    from . import triggers
    where, params = ("AND r.project = ?", [project]) if project else ("", [])
    stated = []
    for row in conn.execute(
            "SELECT r.*, u.text AS quoted_text FROM change_reason r JOIN utterance u ON u.id = r.verbatim_utterance_id "
            f"WHERE r.role = 'reason' AND r.tier = 'stated' AND r.superseded_by IS NULL {where} ORDER BY r.id", params).fetchall():
        if provenance.is_injected_prompt(row["quoted_text"]):
            prefix = next(p for p in provenance.INJECTED_PROMPT_PREFIXES if row["quoted_text"].lstrip().startswith(p))
            stated.append({"id": row["id"], "project": row["project"], "plan_id": row["plan_id"], "node_key": row["node_key"],
                           "utterance_id": row["verbatim_utterance_id"], "starts_with": prefix, "row": dict(row)})
    rejected, unread = [], []
    for proj, plan_id in conn.execute(
            "SELECT DISTINCT r.project, r.plan_id FROM change_reason r WHERE r.role = 'rejected_path' AND r.rule_id = 'R6' "
            f"AND r.superseded_by IS NULL {where} ORDER BY 1, 2", params).fetchall():
        psg = psg_bridge.db_path_for(proj) if proj else None
        found = triggers.misanchored(conn, project=proj, plan_id=plan_id, psg_db_path=psg) if psg else None
        if found is None:
            unread.append(plan_id)
        else:
            rejected += found
    written = 0
    if apply and (stated or rejected):
        with db.transaction(conn):
            for s in stated:
                old = s["row"]
                new = provenance.insert_reason(conn, project=old["project"], plan_id=old["plan_id"], node_key=old["node_key"],
                                               kind=old["kind"], occurred_at=old["occurred_at"], step_id=old["step_id"],
                                               run_id=old["run_id"], recorded_by="system", commit=False)
                provenance.supersede(conn, old["id"], new, commit=False)
                written += 1
            for m in rejected:
                old = m["row"]
                new = provenance.insert_reason(conn, project=old["project"], plan_id=old["plan_id"], node_key=m["to"],
                                               kind=old["kind"], role="rejected_path", occurred_at=old["occurred_at"],
                                               step_id=old["step_id"], run_id=old["run_id"], interpretation=old["interpretation"],
                                               rule_id="R6", recorded_by="system", commit=False)
                provenance.supersede(conn, old["id"], new, commit=False)
                written += 1
    return {"stated": _without_row(stated), "rejected": _without_row(rejected), "graph_unread": unread,
            "written": written, "applied": bool(apply)}


def _without_row(found: list[dict]) -> list[dict]:
    return [{k: v for k, v in x.items() if k != "row"} for x in found]

# ── close-time backstops ─────────────────────────────────────────────────────

def backstop_unstated(conn, *, project: str, plan_id: str, psg_db_path: str | None,
                      commit: bool = False, state: str = "active") -> int:
    """A plan whose task nobody explained gets one explicit `unstated` task
    reason from the system — the gap is recorded, never papered over (task-level
    redesign, step 3a: one per task, not one per changed node). `state="unknown"`
    is the pending close mode (nobody was asked yet). A plan or a session that
    changed no node gets nothing. Returns 1 or 0."""
    changed = _changed(psg_db_path, plan_id)
    if not changed or _live_task_reasons(conn, plan_id):      # nothing changed: no task to explain
        return 0
    run_id = max(c["run_id"] for c in changed)
    provenance.insert_reason(conn, project=project, plan_id=plan_id, node_key=None, kind="technical",
                             run_id=run_id, recorded_by="system", state=state, commit=False)
    if commit:
        conn.commit()
    return 1


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
