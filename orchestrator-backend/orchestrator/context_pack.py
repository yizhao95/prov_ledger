"""context_pack — one bounded read of everything a plan should know about a
node before changing it (DP phase 2, Task 2; spec §5, §6, H2).

`build()` is the ONE constructor: `impact_preflight` (plan time) and
`provledger why` (on demand) both call it, so the two never disagree.

Layer 1 — the node itself and its identity chain (the names it carried before
a rename / move): active constraints (all), rejected paths (last 5), reasons
(last 3; only an explicit `minor` significance is folded into a count), the
latest outcome of the plans that changed it before.
Layer 2 — the blast radius from the consistency card: callers, output
consumers, dtype map, lineage downstream; neighbours one hop downstream come
as counts `[constraints n · rejected m]` unless `neighbors="constraints"`.

`budget_tokens` trims in a fixed order — reasons, then rejected paths, then
neighbour constraints, constraints last — and every trimmed record shows up
as a count with the command that expands it. Nothing is dropped silently.

Every change_reason row that ends up in the pack is a read_hit(moment): the
system told; whether anyone read it is not recorded here (§5.3).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import psg_bridge

CAP_REJECTED = 5
CAP_REASONS = 3
CAP_NEIGHBOR_CONSTRAINTS = 2
CAP_OUTCOMES = 3
TRIM_ORDER = ("reasons", "rejected_paths", "neighbor_constraints", "constraints")


@dataclass
class TargetPack:
    qualified_name: str
    node_key: str | None
    status: str                                  # existing | new
    identity_chain: list[str] = field(default_factory=list)
    constraints: list[dict] = field(default_factory=list)
    rejected_paths: list[dict] = field(default_factory=list)
    reasons: list[dict] = field(default_factory=list)
    prior_outcomes: list[dict] = field(default_factory=list)
    callers: list[str] = field(default_factory=list)
    output_consumers: list[str] = field(default_factory=list)
    dtype_map: dict = field(default_factory=dict)
    lineage_downstream: list[str] = field(default_factory=list)
    upstream_assumptions: list[dict] = field(default_factory=list)
    removed_upstream: list[dict] = field(default_factory=list)   # callees / upstream whose latest event is node_removed
    counts: dict = field(default_factory=dict)   # totals before any cap / trim (minor folded reasons included)


@dataclass
class Pack:
    project: str
    moment: str
    budget_tokens: int
    targets: list[TargetPack] = field(default_factory=list)
    neighbors_counts: dict = field(default_factory=dict)      # qualified_name -> {constraints, rejected_paths, statements?}
    truncated: dict = field(default_factory=dict)              # kind -> records trimmed by the budget
    hints: list[str] = field(default_factory=list)             # "还有 n 条 …，provledger why <qn> --all"
    approx_tokens: int = 0
    generated_at: str = ""
    shown: int = 0                                             # change_reason rows put in the pack (read_hits when recorded)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Pack":
        """Rebuild a Pack from its stored dict (Plans.impact_context.pack)."""
        keys = {f for f in cls.__dataclass_fields__}
        tkeys = {f for f in TargetPack.__dataclass_fields__}
        pack = cls(**{k: v for k, v in d.items() if k in keys and k != "targets"})
        pack.targets = [TargetPack(**{k: v for k, v in t.items() if k in tkeys}) for t in d.get("targets", [])]
        return pack


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _psg_rows(psg, sql: str, params: tuple = ()) -> list:
    if psg is None:
        return []
    try:
        return psg.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _identity_chain(psg, node_key: str | None, qn: str) -> list[str]:
    """Every qualified name this node_key ever carried (snapshots + rename events), newest first."""
    names: list[str] = [qn]
    if node_key:
        for r in _psg_rows(psg, "SELECT DISTINCT qualified_name FROM node_snapshot WHERE node_key = ? ORDER BY run_id DESC", (node_key,)):
            if r[0] and r[0] not in names:
                names.append(r[0])
        for r in _psg_rows(psg, "SELECT payload_json FROM node_event WHERE node_key = ? AND event_type IN ('node_renamed', 'node_moved') ORDER BY run_id DESC", (node_key,)):
            try:
                p = json.loads(r[0] or "{}")
            except ValueError:
                continue
            for v in (p.get("from"), p.get("to")):
                if v and "." in str(v) and v not in names and not str(v).endswith(".py"):
                    names.append(v)
    return names


def _card(psg, qn: str) -> dict:
    rows = _psg_rows(psg, "SELECT cc.card_json FROM consistency_card cc JOIN node n ON n.id = cc.symbol_id "
                          "WHERE n.qualified_name = ? ORDER BY n.id DESC LIMIT 1", (qn,))
    if not rows or not rows[0][0]:
        return {}
    try:
        card = json.loads(rows[0][0])
    except ValueError:
        return {}
    return card if isinstance(card, dict) else {}


def _node_key(psg, qn: str) -> str | None:
    """Exact qualified name first; otherwise a unique dotted-suffix match
    (a plan declares `orchestrator.reasons.fill`, the graph knows it as
    `orchestrator-backend.orchestrator.reasons.fill`). Ambiguous → None."""
    rows = _psg_rows(psg, "SELECT node_key FROM node_snapshot WHERE qualified_name = ? AND node_key <> '' ORDER BY run_id DESC, id DESC LIMIT 1", (qn,))
    if rows:
        return rows[0][0]
    if not qn or "." not in qn:
        return None
    rows = _psg_rows(psg, "SELECT DISTINCT node_key FROM node_snapshot WHERE qualified_name LIKE ? AND node_key <> '' "
                          "AND run_id = (SELECT MAX(run_id) FROM node_snapshot x WHERE x.node_key = node_snapshot.node_key)", ("%." + qn,))
    return rows[0][0] if len(rows) == 1 else None


def _records(conn, project: str, anchors: list[str]) -> list[dict]:
    if not anchors:
        return []
    ph = ",".join("?" * len(anchors))
    return [dict(r) for r in conn.execute(
        f"SELECT r.id, r.node_key, r.plan_id, r.step_id, r.role, r.tier, r.evidence_level, r.rule_id, r.state, r.superseded_by, "
        f"       r.recorded_by, r.significance, r.occurred_at, r.statement, "
        f"       COALESCE(r.interpretation, r.statement, substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start)) AS text "
        f"FROM change_reason_v r LEFT JOIN utterance u ON u.id = r.verbatim_utterance_id "
        f"WHERE r.project = ? AND r.node_key IN ({ph}) ORDER BY r.id DESC", (project, *anchors))]


def _slim(r: dict) -> dict:
    return {"id": r["id"], "role": r["role"], "tier": r["tier"], "evidence_level": r["evidence_level"], "rule_id": r.get("rule_id"),
            "recorded_by": r["recorded_by"], "occurred_at": r["occurred_at"], "plan_id": r["plan_id"],
            "text": (r["text"] or "")[:240]}


def _prior_outcomes(conn, project: str, names: list[str]) -> list[dict]:
    if not names:
        return []
    ph = ",".join("?" * len(names))
    rows = conn.execute(
        f"SELECT e.id, e.plan_id, e.claim, e.channel, o.kind, o.tier, o.value_json, o.observed_at "
        f"FROM expectations e LEFT JOIN outcomes o ON o.id = (SELECT id FROM outcomes WHERE expectation_id = e.id ORDER BY observed_at DESC, id DESC LIMIT 1) "
        f"WHERE e.project = ? AND e.target IN ({ph}) ORDER BY e.id DESC LIMIT ?", (project, *names, CAP_OUTCOMES)).fetchall()
    out = []
    for r in rows:
        try:
            value = json.loads(r[6]) if r[6] else {}
        except ValueError:
            value = {}
        out.append({"expectation_id": r[0], "plan_id": r[1], "claim": r[2], "channel": r[3], "kind": r[4] or "pending",
                    "tier": r[5] or "pending", "signal": value.get("signal") if isinstance(value, dict) else None,
                    "delta_pct": value.get("delta_pct") if isinstance(value, dict) else None, "observed_at": r[7]})
    return out


def _tokens(obj) -> int:
    return len(json.dumps(obj, default=str, ensure_ascii=False)) // 4


def build(conn, *, project: str, targets: list[str], psg_db_path: str | None = None, budget_tokens: int = 1500,
          neighbors: str = "counts", moment: str = "plan", plan_id: str | None = None, session_id: str | None = None,
          step_id: str | None = None, record: bool = True) -> Pack:
    """The pack for `targets` (qualified names or nk_ keys). ONE read-only PSG
    connection for the whole build (H2). `record=False` writes nothing."""
    psg_path = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    psg = None
    if psg_path:
        try:
            psg = psg_bridge.open_ro(psg_path)
        except sqlite3.Error:
            psg = None
    pack = Pack(project=project, moment=moment, budget_tokens=budget_tokens, generated_at=_now())
    shown_ids: list[int] = []
    try:
        for t in targets:
            if not t:
                continue
            if t.startswith("nk_"):
                key = t
                rows = _psg_rows(psg, "SELECT qualified_name FROM node_snapshot WHERE node_key = ? ORDER BY run_id DESC, id DESC LIMIT 1", (key,))
                qn = rows[0][0] if rows else t
            else:
                qn, key = t, _node_key(psg, t)
            tp = TargetPack(qualified_name=qn, node_key=key, status="existing" if key else "new")
            tp.identity_chain = _identity_chain(psg, key, qn)
            anchors = ([key] if key else []) + tp.identity_chain
            recs = _records(conn, project, anchors)
            cons = [r for r in recs if r["role"] == "constraint" and r["state"] == "active" and r["superseded_by"] is None]
            rej = [r for r in recs if r["role"] == "rejected_path"]
            rea = [r for r in recs if r["role"] == "reason"]
            minor = [r for r in rea if r.get("significance") == "minor"]
            rea = [r for r in rea if r.get("significance") != "minor"]
            tp.counts = {"constraints": len(cons), "rejected_paths": len(rej), "reasons": len(rea) + len(minor), "reasons_minor": len(minor)}
            tp.constraints = [_slim(r) for r in cons]
            tp.rejected_paths = [_slim(r) for r in rej[:CAP_REJECTED]]
            tp.reasons = [_slim(r) for r in rea[:CAP_REASONS]]
            tp.prior_outcomes = _prior_outcomes(conn, project, tp.identity_chain)
            card = _card(psg, qn)
            tp.callers = list(card.get("callers") or [])
            tp.output_consumers = list(card.get("output_consumers") or [])
            tp.dtype_map = dict(card.get("dtype_map") or {})
            tp.lineage_downstream = list(card.get("lineage_downstream") or [])
            tp.upstream_assumptions = [{"table": x} for x in (card.get("reads") or [])]
            for up in list(card.get("callees") or []) + list(card.get("lineage_upstream") or []):
                uk = _node_key(psg, up)
                if not uk:
                    continue
                last = _psg_rows(psg, "SELECT id, event_type, run_id FROM node_event WHERE node_key = ? ORDER BY run_id DESC, seq DESC LIMIT 1", (uk,))
                if last and last[0][1] == "node_removed":
                    tp.removed_upstream.append({"qualified_name": up, "node_key": uk, "event_id": last[0][0], "run_id": last[0][2]})
            for nb in tp.output_consumers:
                if nb in pack.neighbors_counts:
                    continue
                nk = _node_key(psg, nb)
                nrecs = _records(conn, project, [x for x in (nk, nb) if x]) if (nk or nb) else []
                ncons = [r for r in nrecs if r["role"] == "constraint" and r["state"] == "active" and r["superseded_by"] is None]
                entry = {"constraints": len(ncons), "rejected_paths": sum(1 for r in nrecs if r["role"] == "rejected_path")}
                if neighbors == "constraints" and ncons:
                    entry["statements"] = [_slim(r) for r in ncons[:CAP_NEIGHBOR_CONSTRAINTS]]
                    if len(ncons) > CAP_NEIGHBOR_CONSTRAINTS:
                        pack.truncated["neighbor_constraints"] = pack.truncated.get("neighbor_constraints", 0) + len(ncons) - CAP_NEIGHBOR_CONSTRAINTS
                pack.neighbors_counts[nb] = entry
            # caps beyond the fixed per-node limits count as truncated too
            if len(rej) > CAP_REJECTED:
                pack.truncated["rejected_paths"] = pack.truncated.get("rejected_paths", 0) + len(rej) - CAP_REJECTED
            if len(rea) > CAP_REASONS:
                pack.truncated["reasons"] = pack.truncated.get("reasons", 0) + len(rea) - CAP_REASONS
            if minor:
                pack.truncated["reasons_minor"] = pack.truncated.get("reasons_minor", 0) + len(minor)
            pack.targets.append(tp)
    finally:
        if psg is not None:
            psg.close()
    _trim_to_budget(pack)
    for tp in pack.targets:
        shown_ids += [r["id"] for r in tp.constraints + tp.rejected_paths + tp.reasons]
    for entry in pack.neighbors_counts.values():
        shown_ids += [r["id"] for r in entry.get("statements", [])]
    pack.shown = len(set(shown_ids))
    pack.hints = _hints(pack)
    pack.approx_tokens = _tokens(pack.as_dict())
    if record and shown_ids:
        write_read_hits(conn, project=project, reason_ids=sorted(set(shown_ids)), moment=moment, plan_id=plan_id,
                        session_id=session_id, step_id=step_id)
    return pack


def _trim_to_budget(pack: Pack) -> None:
    """Drop records in TRIM_ORDER until the pack fits; count each drop."""
    def over() -> bool:
        return _tokens(pack.as_dict()) > pack.budget_tokens

    for kind in TRIM_ORDER:
        while over():
            dropped = False
            if kind == "neighbor_constraints":
                for entry in pack.neighbors_counts.values():
                    if entry.get("statements"):
                        entry["statements"].pop()
                        dropped = True
                        break
            else:
                for tp in reversed(pack.targets):
                    lst = getattr(tp, kind)
                    if lst:
                        lst.pop()
                        dropped = True
                        break
            if not dropped:
                break
            pack.truncated[kind] = pack.truncated.get(kind, 0) + 1


def _hints(pack: Pack) -> list[str]:
    out = []
    names = pack.targets[0].qualified_name if pack.targets else "<node>"
    labels = {"reasons": "reasons", "rejected_paths": "rejected paths", "neighbor_constraints": "neighbour constraints",
              "constraints": "constraints", "reasons_minor": "minor reasons"}
    for kind, n in pack.truncated.items():
        if n:
            out.append(f"还有 {n} 条 {labels.get(kind, kind)} 未展开，`provledger why {names} --all`")
    return out


def write_read_hits(conn, *, project: str, reason_ids: list[int], moment: str, plan_id: str | None = None,
                    session_id: str | None = None, step_id: str | None = None, injected_chars: int | None = None,
                    commit: bool = True) -> int:
    """One read_hit per record shown; the same record in the same plan at the
    same moment is not counted twice (a recomputed pack is not a new showing).
    Returns the rows written."""
    n = 0
    for rid in reason_ids:
        if plan_id is not None:
            dup = conn.execute("SELECT 1 FROM read_hit WHERE reason_id = ? AND plan_id = ? AND moment = ? LIMIT 1", (rid, plan_id, moment)).fetchone()
            if dup:
                continue
        conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, session_id, step_id, moment, injected_chars) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (rid, project, plan_id, session_id, step_id, moment, injected_chars))
        n += 1
    if commit:
        conn.commit()
    return n
