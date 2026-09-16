"""triggers — deterministic reason rules R1–R6 (DP phase 1, Task 5; spec §4).

When a reviewed plan closes, every node its runs touched is tried against the
rule table IN ORDER; the first rule that recognises the change writes a
`derived` reason carrying the rule id and its basis (recorded_by system), and
the node is not asked about at close time (C1). A node no rule recognises and
that the plan changed becomes a close-time slot (C2). Every verdict — auto,
ask, or silent when the project closes in `pending` mode — is one trigger_log
row (C4), so the false-trigger and miss rates can be computed later.

  R0 user_words          the user's own words name the node → STATED, the sentence as the span (phase 2)
  R1 test_fixed          a test failed then passed in this plan and names the node's file
  R2 gate_response       the previous review of this project failed a gate on this node
  R3 upstream_drift      a drift decision on a dataset the node reads fell in the plan window
  R4 pure_refactor       identity kept (matched), renamed or moved, struct_sig unchanged
  R5 constraint_covered  an active constraint is anchored on the node
  R6 deviation_recovered a failed step recovered by a deviation → a rejected_path (command + error tail)

Nothing here reads a model; a rule is a function of the ledger and the graph.
"""
from __future__ import annotations

import sqlite3

import json
import re
from dataclasses import dataclass
from typing import Callable

from . import constraints, db, provenance, psg_bridge

ERROR_TAIL = 300
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


@dataclass
class Ctx:
    conn: object
    project: str
    plan_id: str
    psg_db_path: str | None
    plan: dict
    steps: list[dict]
    deviations: list[dict]
    touched: dict[str, dict]          # node_key -> {node_key, qualified_name, node_type, event_types, run_id, file_path}


@dataclass(frozen=True)
class Rule:
    id: str
    describe: str
    check: Callable[[Ctx, dict], str | None]   # basis when the rule recognises the node, else None


# ── the material a rule may look at ──────────────────────────────────────────

def touched_nodes(psg_db_path: str | None, plan_id: str) -> dict[str, dict]:
    """Every node with an event in the plan's runs: {node_key: {…, event_types, payloads, file_path}}."""
    rows = psg_bridge._query(psg_db_path, """
        SELECT e.node_key, e.event_type, e.run_id, e.payload_json,
               (SELECT qualified_name FROM node_snapshot s WHERE s.node_key = e.node_key ORDER BY s.run_id DESC, s.id DESC LIMIT 1) AS qn,
               (SELECT node_type FROM node_snapshot s WHERE s.node_key = e.node_key ORDER BY s.run_id DESC, s.id DESC LIMIT 1) AS nt,
               (SELECT file_path FROM node_snapshot s WHERE s.node_key = e.node_key ORDER BY s.run_id DESC, s.id DESC LIMIT 1) AS fp
        FROM node_event e JOIN analysis_run a ON a.id = e.run_id
        WHERE a.plan_id = ? AND e.node_key IS NOT NULL ORDER BY e.node_key, e.run_id, e.seq""", (plan_id,))
    out: dict[str, dict] = {}
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except ValueError:
            payload = {}
        d = out.setdefault(r["node_key"], {"node_key": r["node_key"], "qualified_name": r["qn"], "node_type": r["nt"],
                                           "file_path": r["fp"], "event_types": [], "payloads": [], "run_id": r["run_id"]})
        d["event_types"].append(r["event_type"])
        d["payloads"].append(payload)
        d["run_id"] = max(d["run_id"], r["run_id"])
    return out


def _local(node: dict) -> str:
    return (node.get("qualified_name") or "").split(".")[-1].split(":")[-1]


def _mentions(text: str | None, node: dict) -> bool:
    if not text:
        return False
    fp, local = node.get("file_path"), _local(node)
    return bool((fp and fp in text) or (local and re.search(rf"\b{re.escape(local)}\b", text)))


def _ordered_steps(ctx: Ctx) -> list[dict]:
    return sorted(ctx.steps, key=lambda s: (s.get("started_at") or "", s.get("execution_order") or 0, s["step_id"]))


# ── R0: the user's own words ─────────────────────────────────────────────────
# A local name that is too short or too common would hit every second sentence.
R0_STOPWORDS = frozenset(
    "run main load save test init setup data value item index count name path file "
    # DP phase 2b (Task 0b): the words an instruction is made of — a sentence saying "plan / step /
    # merge / review" is about the work, not about a function that happens to carry that name
    "plan plans step steps task tasks merge push pull commit review build check update close open "
    "start stop list show read write note mark eval apply fill create delete insert select query "
    "report print parse format render handle process generate collect serve view graph node hint judge".split())
R0_MIN_LOCAL = 4
PASSIVE_EVENTS = frozenset({"node_matched", "identity_asserted", "identity_ambiguous"})   # touched, not changed
R0_MAX_NODES_PER_SENTENCE = 3
_SENTENCE_END = re.compile(r"(?<=[。！？!?])|(?<=\.)(?=\s|$)|\n")


def sentences(text: str) -> list[tuple[int, int]]:
    """(start, end) of every sentence — split at 。！？!? and at a '.' followed
    by whitespace / the end (so 'rollup.py' stays whole); whitespace trimmed."""
    out, pos = [], 0
    for m in _SENTENCE_END.finditer(text):
        end = m.end()
        if end > pos:
            out.append((pos, end))
        pos = end
    if pos < len(text):
        out.append((pos, len(text)))
    trimmed = []
    for s, e in out:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            trimmed.append((s, e))
    return trimmed


def _literal(name: str) -> re.Pattern:
    """Case-sensitive, `_` intact: the name must not be glued to an identifier char."""
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")


def r0_names(node: dict) -> list[str]:
    """The literals that count for a node under the local-name rule: its local
    name (≥ 4 chars, not a stopword) and its qualified name. The file's basename
    is a separate rule since DP phase 2b (FL-066): it anchors to the nodes the
    plan changed in that file — see r0_basename."""
    names = []
    local = _local(node)
    if len(local) >= R0_MIN_LOCAL and local not in R0_STOPWORDS:
        names.append(local)
    qn = node.get("qualified_name") or ""
    if qn and qn != local:
        names.append(qn)
    return names


def r0_basename(node: dict) -> str:
    fp = node.get("file_path") or ""
    return fp.rsplit("/", 1)[-1] if fp else ""


def candidate_utterances(ctx: Ctx) -> list[dict]:
    """The words that may explain this plan: utterances attributed to it, the
    project's utterances inside the plan window, and everything said in the
    same session(s) as those (before a plan exists the hook cannot attribute)."""
    created = ctx.plan.get("created_at") or "0000-00-00 00:00:00"
    completed = ctx.plan.get("completed_at") or "9999-12-31 23:59:59"
    if str(ctx.plan_id).startswith("session:") and ctx.plan.get("session_id"):     # a session's placeholder plan: only what was said in that session
        return [dict(r) for r in ctx.conn.execute(
            "SELECT id, session_id, text FROM utterance WHERE session_id = ? OR plan_id = ? ORDER BY id",
            (ctx.plan["session_id"], ctx.plan_id))]
    # DP phase 2b (FL-069): Plans.session_id — everything said in the session that
    # published the plan counts, whether or not it carries a plan_id or predates created_at
    return [dict(r) for r in ctx.conn.execute(
        "SELECT id, session_id, text FROM utterance WHERE plan_id = ? "
        "OR (project = ? AND occurred_at BETWEEN ? AND ?) "
        "OR session_id IN (SELECT session_id FROM utterance WHERE plan_id = ?) "
        "OR (? IS NOT NULL AND session_id = ?) ORDER BY id",
        (ctx.plan_id, ctx.project, created, completed, ctx.plan_id, ctx.plan.get("session_id"), ctx.plan.get("session_id")))]


def _sentence_hits(ctx: Ctx) -> dict:
    """{(utterance_id, start, end): set(node_key)} over every candidate sentence — computed once per plan."""
    cache = getattr(ctx, "_r0_hits", None)
    if cache is not None:
        return cache
    pats = {key: [_literal(n) for n in r0_names(node)] for key, node in ctx.touched.items()}
    hits: dict = {}
    for u in candidate_utterances(ctx):
        for s, e in sentences(u["text"]):
            sent = u["text"][s:e]
            keys = {key for key, ps_ in pats.items() if any(p.search(sent) for p in ps_)}
            if keys:
                hits[(u["id"], s, e)] = keys
    ctx._r0_hits = hits
    return hits


def _basename_hits(ctx: Ctx) -> dict:
    """{(utterance_id, start, end): {node_key: basename}} — a sentence naming a file
    anchors to the nodes the plan CHANGED in that file (FL-066): never to the
    untouched ones, and never subject to the "> 3 nodes" limit."""
    cache = getattr(ctx, "_r0_base_hits", None)
    if cache is not None:
        return cache
    by_base: dict[str, list[str]] = {}
    for key, node in ctx.touched.items():
        base = r0_basename(node)
        if base:
            by_base.setdefault(base, []).append(key)
    hits: dict = {}
    if by_base:
        pats = {base: _literal(base) for base in by_base}
        changed_cache: dict[str, set] = {}
        for u in candidate_utterances(ctx):
            for s, e in sentences(u["text"]):
                sent = u["text"][s:e]
                for base, pat in pats.items():
                    if not pat.search(sent):
                        continue
                    if base not in changed_cache:
                        changed_cache[base] = {n["node_key"] for n in psg_bridge.changed_in_file(ctx.psg_db_path, ctx.plan_id, base)}
                    for key in changed_cache[base]:
                        hits.setdefault((u["id"], s, e), {})[key] = base
    ctx._r0_base_hits = hits
    return hits


def r0_user_words(ctx: Ctx, node: dict):
    """The first specific sentence that names the node → (utterance_id, start,
    end, basis); a sentence naming more than 3 nodes is generic and is skipped
    (the ambiguity is logged by evaluate). None when no sentence names it."""
    for (uid, s, e), keys in sorted(_sentence_hits(ctx).items()):
        if node["node_key"] in keys and len(keys) <= R0_MAX_NODES_PER_SENTENCE:
            return (uid, s, e, f"R0: the user's words name {_local(node) or node.get('qualified_name')} (utterance {uid})")
    for (uid, s, e), keys in sorted(_basename_hits(ctx).items()):
        if node["node_key"] in keys:
            return (uid, s, e, f"R0: the user's words name the file {keys[node['node_key']]} (utterance {uid})")
    return None


def r0_ambiguous(ctx: Ctx, node: dict) -> str | None:
    """The basis of the generic sentences that name the node, if any."""
    ns = [len(keys) for (uid, s, e), keys in _sentence_hits(ctx).items()
          if node["node_key"] in keys and len(keys) > R0_MAX_NODES_PER_SENTENCE]
    return f"R0: sentence names {max(ns)} nodes" if ns else None


# ── R1..R5 ────────────────────────────────────────────────────────────────────

def r1_test_fixed(ctx: Ctx, node: dict) -> str | None:
    """A step whose log shows a pytest failure, then a later step whose log
    shows pytest passing and names the node's file or local name."""
    failed_at = None
    for s in _ordered_steps(ctx):
        if s.get("is_review"):
            continue
        log = s.get("log_context") or ""
        if "pytest" not in log:
            continue
        if s["status"] == "FAILED" or " failed" in log:
            if failed_at is None:
                failed_at = s["step_id"]
            continue
        if failed_at and s["status"] == "COMPLETED" and " passed" in log and _mentions(log, node):
            return f"test failed in {failed_at} then passed in {s['step_id']} ({node.get('file_path') or _local(node)})"
    return None


def r2_gate_response(ctx: Ctx, node: dict) -> str | None:
    """The previous reviewed plan of this project logged a failing gate that names the node."""
    prev = ctx.conn.execute(
        "SELECT plan_id FROM Plans WHERE project = ? AND plan_id <> ? AND created_at < ? AND status = 'COMPLETED' "
        "ORDER BY created_at DESC LIMIT 1", (ctx.project, ctx.plan_id, ctx.plan.get("created_at") or "")).fetchone()
    if not prev:
        return None
    for s in db.get_steps(ctx.conn, prev[0]):
        if not s.get("is_review"):
            continue
        for line in (s.get("log_context") or "").splitlines():
            low = line.lower()
            if ("[fail]" in low or "fail:" in low or "failed" in low) and _mentions(line, node):
                return f"responds to a failed gate in {prev[0]}'s review: {line.strip()[:160]}"
    return None


def r3_upstream_drift(ctx: Ctx, node: dict) -> str | None:
    """A drift decision (llm_decisions) on a dataset the node reads, inside the plan window."""
    created = ctx.plan.get("created_at") or "0000"
    completed = ctx.plan.get("completed_at") or "9999"
    try:
        rows = ctx.conn.execute(
            "SELECT dataset, column_name, drift_kind, created_at FROM llm_decisions WHERE drift_kind IS NOT NULL "
            "AND created_at BETWEEN ? AND ? ORDER BY id", (created, completed)).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    card = psg_bridge.card_of(ctx.psg_db_path, node.get("qualified_name") or "")
    reads = {str(x).lower() for x in (card.get("reads") or [])} | {str(x).lower() for x in (card.get("lineage_upstream") or [])}
    qn = (node.get("qualified_name") or "").lower()
    for ds, col, kind, at in rows:
        d = (ds or "").lower()
        if d and (d in reads or d in qn):
            return f"upstream dataset {ds}{'.' + col if col else ''} drifted ({kind}) at {at}"
    return None


def r4_pure_refactor(ctx: Ctx, node: dict) -> str | None:
    """Identity kept (node_matched) and renamed / moved, with no struct_sig change."""
    ets = node.get("event_types") or []
    if "node_matched" not in ets or not ({"node_renamed", "node_moved"} & set(ets)):
        return None
    for et, p in zip(ets, node.get("payloads") or []):
        if et == "node_changed" and "struct_sig" in (p.get("changed") or []):
            return None
    via = next((p.get("via") for et, p in zip(ets, node.get("payloads") or []) if et == "node_matched"), "?")
    what = " + ".join(sorted({"node_renamed", "node_moved"} & set(ets)))
    return f"pure refactor: identity kept via {via}, {what}, struct_sig unchanged"


def r5_constraint_covered(ctx: Ctx, node: dict) -> str | None:
    hits = constraints.anchored_constraints(ctx.conn, ctx.project, [node["node_key"]], [node.get("qualified_name")])
    if not hits:
        return None
    c = hits[0]
    return f"covered by active constraint #{c.get('id')}: {(c.get('statement') or '')[:160]}"


def _existing_rule_reason(conn, project: str, node_key: str, rule_id: str, basis: str) -> int | None:
    """The id of an identical rule record already on this node, if there is one.

    A constraint being in force is a fact about the NODE, not about each plan
    that meets it. Writing it once per plan produced 16 identical `derived` rows
    on this repo's own compute_etag — true, repetitive, and unreadable. The
    later plans record that the fact was SURFACED to them (read_hit), which is
    the distinction read_hit exists to carry."""
    try:
        row = conn.execute(
            "SELECT id FROM change_reason WHERE project = ? AND node_key = ? AND rule_id = ? "
            "AND interpretation = ? AND state = 'active' AND superseded_by IS NULL ORDER BY id LIMIT 1",
            (project, node_key, rule_id, basis)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


RULES: tuple[Rule, ...] = (
    Rule("R0", "the user's own words name the node (stated, verbatim span)", r0_user_words),
    Rule("R1", "a test failed then passed in this plan and names the node", r1_test_fixed),
    Rule("R2", "the previous review failed a gate on the node", r2_gate_response),
    Rule("R3", "an upstream dataset drifted inside the plan window", r3_upstream_drift),
    Rule("R4", "pure refactor: identity kept, struct_sig unchanged", r4_pure_refactor),
    Rule("R5", "an active constraint is anchored on the node", r5_constraint_covered),
)


# ── R6: rejected paths ────────────────────────────────────────────────────────

def _anchor(text: str | None, nodes: list[dict]) -> str | None:
    toks = {t.lower() for t in _TOKEN_RE.findall(text or "")}
    for n in nodes:
        local = _local(n).lower()
        if local and local in toks:
            return n["node_key"]
    return None


def r6_candidates(ctx: Ctx) -> list[tuple[str, str | None, str]]:
    """(text, step_id, anchor_text): one per FAILED non-review step — the
    command (step description), its failure reason, the justification of the
    deviation that recovered it, and the error tail — plus one per deviation
    whose target step did not fail."""
    devs_by_step: dict = {}
    for d in ctx.deviations:
        devs_by_step.setdefault(d.get("target_step_id"), []).append(d)
    out = []
    failed_steps = set()
    for s in ctx.steps:
        if s["status"] != "FAILED" or s.get("is_review"):
            continue
        failed_steps.add(s["step_id"])
        tail = (s.get("log_context") or "").strip()[-ERROR_TAIL:]
        parts = [(s.get("description") or s["step_id"]).strip()]
        if s.get("failure_reason") and s["failure_reason"].strip() not in parts:
            parts.append(s["failure_reason"].strip())
        for d in devs_by_step.get(s["step_id"], []):
            j = (d.get("justification") or "").strip()
            if j and j not in parts:
                parts.append("deviation: " + j)
        text = " — ".join(parts) + ("\n" + tail if tail else "")
        out.append((text, s["step_id"], " ".join(parts + [tail])))
    for step_id, ds in devs_by_step.items():
        if step_id in failed_steps:
            continue
        step = db.get_step(ctx.conn, step_id) if step_id else None
        for d in ds:
            anchor_text = " ".join(filter(None, [step.get("description") if step else "", d.get("justification")]))
            out.append(((d.get("justification") or "").strip(), step_id, anchor_text))
    return [c for c in out if c[0]]


def rejected_paths(conn, *, project: str, plan_id: str, psg_db_path: str | None, commit: bool = False) -> int:
    """R6: deviation justifications and failed steps become derived
    rejected_path rows (rule R6) anchored to the changed node they name.
    Idempotent per (plan, text)."""
    ctx = _ctx(conn, project, plan_id, psg_db_path)
    nodes = list(ctx.touched.values())
    existing = {r["interpretation"] for r in provenance.reasons_for_plan(conn, plan_id, role="rejected_path")}
    run_id = max((n["run_id"] for n in nodes), default=None)
    n = 0
    for text, step_id, anchor_text in r6_candidates(ctx):
        if text in existing:
            continue
        key = _anchor(anchor_text, nodes)
        provenance.insert_reason(conn, project=project, plan_id=plan_id, node_key=key, kind="technical", role="rejected_path",
                                 run_id=run_id, step_id=step_id, interpretation=text, rule_id="R6", recorded_by="system",
                                 commit=False)
        conn.execute("INSERT INTO trigger_log (project, plan_id, node_key, path, rule_id, verdict, basis) VALUES (?, ?, ?, 'code', 'R6', 'auto', ?)",
                     (project, plan_id, key, text[:400]))
        existing.add(text)
        n += 1
    if commit:
        conn.commit()
    return n


# ── evaluate ──────────────────────────────────────────────────────────────────

def _session_plan(conn, plan_id: str) -> dict:
    """DP phase 2 (Task 7b): `session:<sid>` is not a Plans row — its window and
    project come from session_run, its words from that session only."""
    sid = plan_id.split(":", 1)[1]
    r = conn.execute("SELECT project, started_at, ended_at FROM session_run WHERE session_id = ?", (sid,)).fetchone()
    return {"plan_id": plan_id, "session_id": sid, "project": r[0] if r else None,
            "created_at": r[1] if r else None, "completed_at": r[2] if r else None}


def _ctx(conn, project, plan_id, psg_db_path) -> Ctx:
    plan = db.get_plan(conn, plan_id) or {}
    if not plan and str(plan_id).startswith("session:"):
        plan = _session_plan(conn, plan_id)
    return Ctx(conn=conn, project=project, plan_id=plan_id, psg_db_path=psg_db_path,
               plan=plan, steps=db.get_steps(conn, plan_id),
               deviations=db.get_deviations(conn, plan_id), touched=touched_nodes(psg_db_path, plan_id))


def _has_rule_reason(conn, plan_id: str, node_key: str) -> bool:
    """A rule already answered for (plan, node) — derived (R1–R5) or stated (R0)."""
    return conn.execute("SELECT 1 FROM change_reason WHERE plan_id = ? AND node_key = ? AND role = 'reason' "
                        "AND rule_id IS NOT NULL AND rule_id <> 'legacy' LIMIT 1", (plan_id, node_key)).fetchone() is not None


def _logged(conn, plan_id: str, node_key: str) -> bool:
    return conn.execute("SELECT 1 FROM trigger_log WHERE plan_id = ? AND node_key = ? AND path = 'code' "
                        "AND verdict IN ('auto', 'ask', 'silent') AND (rule_id IS NULL OR rule_id <> 'R6') LIMIT 1",
                        (plan_id, node_key)).fetchone() is not None


def evaluate(conn, *, project: str, plan_id: str, psg_db_path: str | None, ask: bool = True, commit: bool = False) -> dict:
    """Try R0..R5 on every node the plan's runs touched. First hit → a stated
    (R0, the user's words) or derived reason + trigger_log auto; no hit on a node the plan CHANGED → trigger_log
    ask (or silent when the project closes in pending mode); no hit on a
    merely-touched node → nothing (it was never a slot). Idempotent."""
    ctx = _ctx(conn, project, plan_id, psg_db_path)
    changed = {c["node_key"] for c in psg_bridge.changed_node_keys(psg_db_path, plan_id)}
    result = {"auto": 0, "ask": 0, "silent": 0, "by_rule": {r.id: 0 for r in RULES}, "nodes": []}
    for key, node in ctx.touched.items():
        if _has_rule_reason(conn, plan_id, key) or _logged(conn, plan_id, key):
            continue
        # DP phase 2b (Task 0b): a node the plan merely matched (or whose identity was
        # asserted) did not change — it is not a slot and no rule may anchor to it;
        # dp2b-t1 had R0 pin "plan / step / merge" to methods of that name
        if not (set(node.get("event_types") or []) - PASSIVE_EVENTS):
            continue
        hit = None
        for rule in RULES:
            found = rule.check(ctx, node)
            if found:
                hit = (rule.id, found)
                break
        amb = r0_ambiguous(ctx, node)
        if amb and not (hit and hit[0] == "R0"):
            conn.execute("INSERT INTO trigger_log (project, plan_id, node_key, path, rule_id, verdict, basis) VALUES (?, ?, ?, 'code', 'R0', 'ambiguous', ?)",
                         (project, plan_id, key, amb))
        if hit:
            rule_id, found = hit
            if isinstance(found, tuple):                                    # R0: (utterance_id, start, end, basis) → stated
                uid, start, end, basis = found
                provenance.insert_reason(conn, project=project, plan_id=plan_id, node_key=key, kind="technical",
                                         run_id=node["run_id"], verbatim=(uid, start, end), rule_id=rule_id,
                                         recorded_by="system", commit=False)
            else:
                basis = found
                seen = _existing_rule_reason(conn, project, key, rule_id, basis)
                if seen is None:
                    provenance.insert_reason(conn, project=project, plan_id=plan_id, node_key=key, kind="technical",
                                             run_id=node["run_id"], interpretation=basis, rule_id=rule_id, recorded_by="system", commit=False)
                else:
                    # the fact is already on the node — this plan was SHOWN it
                    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment) VALUES (?, ?, ?, 'close')",
                                 (seen, project, plan_id))
                    basis = f"{basis} (already recorded as #{seen}; surfaced to this plan)"
            conn.execute("INSERT INTO trigger_log (project, plan_id, node_key, path, rule_id, verdict, basis) VALUES (?, ?, ?, 'code', ?, 'auto', ?)",
                         (project, plan_id, key, rule_id, basis))
            result["auto"] += 1
            result["by_rule"][rule_id] += 1
            result["nodes"].append({"node_key": key, "qualified_name": node["qualified_name"], "rule_id": rule_id, "basis": basis})
        elif key in changed:
            verdict = "ask" if ask else "silent"
            conn.execute("INSERT INTO trigger_log (project, plan_id, node_key, path, rule_id, verdict, basis) VALUES (?, ?, ?, 'code', NULL, ?, ?)",
                         (project, plan_id, key, verdict, "no rule recognised the change"))
            result[verdict] += 1
    if commit:
        conn.commit()
    return result


def auto_filled(conn, plan_id: str) -> list[dict]:
    """What the rules filled for a plan — shown in the reason-slots checklist."""
    return [dict(r) for r in conn.execute(
        "SELECT r.node_key, r.rule_id, COALESCE(r.interpretation, substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start)) AS basis "
        "FROM change_reason r LEFT JOIN utterance u ON u.id = r.verbatim_utterance_id "
        "WHERE r.plan_id = ? AND r.role = 'reason' AND r.tier IN ('derived', 'stated') AND r.rule_id IS NOT NULL ORDER BY r.id", (plan_id,))]
