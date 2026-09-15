"""triggers — deterministic reason rules R1–R6 (DP phase 1, Task 5; spec §4).

When a reviewed plan closes, every node its runs touched is tried against the
rule table IN ORDER; the first rule that recognises the change writes a
`derived` reason carrying the rule id and its basis (recorded_by system), and
the node is not asked about at close time (C1). A node no rule recognises and
that the plan changed becomes a close-time slot (C2). Every verdict — auto,
ask, or silent when the project closes in `pending` mode — is one trigger_log
row (C4), so the false-trigger and miss rates can be computed later.

  R1 test_fixed          a test failed then passed in this plan and names the node's file
  R2 gate_response       the previous review of this project failed a gate on this node
  R3 upstream_drift      a drift decision on a dataset the node reads fell in the plan window
  R4 pure_refactor       identity kept (matched), renamed or moved, struct_sig unchanged
  R5 constraint_covered  an active constraint is anchored on the node
  R6 deviation_recovered a failed step recovered by a deviation → a rejected_path (command + error tail)

Nothing here reads a model; a rule is a function of the ledger and the graph.
"""
from __future__ import annotations

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


RULES: tuple[Rule, ...] = (
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

def _ctx(conn, project, plan_id, psg_db_path) -> Ctx:
    return Ctx(conn=conn, project=project, plan_id=plan_id, psg_db_path=psg_db_path,
               plan=db.get_plan(conn, plan_id) or {}, steps=db.get_steps(conn, plan_id),
               deviations=db.get_deviations(conn, plan_id), touched=touched_nodes(psg_db_path, plan_id))


def _has_rule_reason(conn, plan_id: str, node_key: str) -> bool:
    return conn.execute("SELECT 1 FROM change_reason WHERE plan_id = ? AND node_key = ? AND role = 'reason' AND tier = 'derived' "
                        "AND rule_id IS NOT NULL AND rule_id <> 'legacy' LIMIT 1", (plan_id, node_key)).fetchone() is not None


def _logged(conn, plan_id: str, node_key: str) -> bool:
    return conn.execute("SELECT 1 FROM trigger_log WHERE plan_id = ? AND node_key = ? AND path = 'code' AND (rule_id IS NULL OR rule_id <> 'R6') LIMIT 1",
                        (plan_id, node_key)).fetchone() is not None


def evaluate(conn, *, project: str, plan_id: str, psg_db_path: str | None, ask: bool = True, commit: bool = False) -> dict:
    """Try R1..R5 on every node the plan's runs touched. First hit → a derived
    reason + trigger_log auto; no hit on a node the plan CHANGED → trigger_log
    ask (or silent when the project closes in pending mode); no hit on a
    merely-touched node → nothing (it was never a slot). Idempotent."""
    ctx = _ctx(conn, project, plan_id, psg_db_path)
    changed = {c["node_key"] for c in psg_bridge.changed_node_keys(psg_db_path, plan_id)}
    result = {"auto": 0, "ask": 0, "silent": 0, "by_rule": {r.id: 0 for r in RULES}, "nodes": []}
    for key, node in ctx.touched.items():
        if _has_rule_reason(conn, plan_id, key) or _logged(conn, plan_id, key):
            continue
        hit = None
        for rule in RULES:
            basis = rule.check(ctx, node)
            if basis:
                hit = (rule.id, basis)
                break
        if hit:
            rule_id, basis = hit
            provenance.insert_reason(conn, project=project, plan_id=plan_id, node_key=key, kind="technical",
                                     run_id=node["run_id"], interpretation=basis, rule_id=rule_id, recorded_by="system", commit=False)
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
        "SELECT node_key, rule_id, interpretation AS basis FROM change_reason WHERE plan_id = ? AND role = 'reason' "
        "AND tier = 'derived' AND rule_id IS NOT NULL ORDER BY id", (plan_id,))]
