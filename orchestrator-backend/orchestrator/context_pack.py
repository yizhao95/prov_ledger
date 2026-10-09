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

from . import provenance, psg_bridge

CAP_CALLERS = 12          # a card's callers list is folded to this many (a hot function has hundreds of test callers)
CAP_REJECTED = 5
CAP_REASONS = 3
CAP_NEIGHBOR_CONSTRAINTS = 2
CAP_OUTCOMES = 3
CAP_TEXT = 240            # characters of one record's text that travel in a pack
TRIM_ORDER = ("reasons", "rejected_paths", "neighbor_constraints", "constraints")

# FL-153: a bound that the caller cannot lift is a bound the caller cannot be
# told about honestly. `provledger why --all` and `--impact` used to change only
# what why.py PRINTED; build kept applying the constants below and computed
# `truncated` (and therefore the footer) from them. The result was a read that
# recommended the flag it had just been given, and a `callers 12` sitting next
# to a `callers 38` in the same output — the first being what the cap kept, the
# second what it dropped, neither being the 50 that exist.
#
# So the caps are now DATA. `build(caps=...)` overrides any of them, a value of
# NO_CAP means "the caller asked for all of it", and everything downstream —
# the lists, `truncated`, the hints — follows from the caps this pack actually
# ran under (`Pack.caps`). An uncapped kind is also exempt from the budget fold:
# folding away the very thing a reader named would be the same lie in a
# different place.
NO_CAP = 10 ** 9
# `None` is "no cap at build time, but the budget may still fold it"; NO_CAP is
# "the caller asked for all of it", which the budget must then leave alone. The
# two are not the same thing and conflating them would silently turn every
# default read into an unfoldable one.
DEFAULT_CAPS = {
    "callers": CAP_CALLERS,
    "rejected_paths": CAP_REJECTED,
    "reasons": CAP_REASONS,
    "constraints": None,                   # every active constraint travels; only the budget may cut them
    "neighbor_constraints": CAP_NEIGHBOR_CONSTRAINTS,
    "lineage_downstream": None,            # never capped at build time — only folded when over budget
    "dtype_map": None,
}


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
    truncated: dict = field(default_factory=dict)              # kind -> records this pack does NOT carry (cap + budget)
    caps: dict = field(default_factory=dict)                   # kind -> the bound this pack ran under (NO_CAP = lifted)
    hints: list[str] = field(default_factory=list)             # "n more … — provledger why <qn> --all"
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
    if not qn:
        return None
    # A BARE name is the consistency card's own spelling (FL-047: cards record
    # callees as `discount_rate`, not `pkg.rollup.discount_rate`). Before this it
    # resolved to nothing, which meant removed_upstream could never fire on a real
    # graph. It resolves when the suffix is unique and stays unresolved when it is
    # not — guessing which `__init__` a card meant is worse than saying nothing.
    rows = _psg_rows(psg, "SELECT DISTINCT node_key FROM node_snapshot WHERE qualified_name LIKE ? AND node_key <> '' "
                          "AND run_id = (SELECT MAX(run_id) FROM node_snapshot x WHERE x.node_key = node_snapshot.node_key)", ("%." + qn,))
    return rows[0][0] if len(rows) == 1 else None


def _retired_declarations(conn, psg, project: str, qn: str | None) -> list[dict]:
    """Declared nodes that pointed AT `qn` and have since been retired.

    FL-083: `removed_upstream` walks the target's consistency card, and a node
    that is gone has already left that card — so "your upstream was removed",
    the one thing most worth saying, could never be said. For declarations the
    link outlives the node: `declared_node` is append-only, so the retired row
    still names what it constrained. That is the half of FL-083 this phase can
    close honestly; the code half (who called a deleted function) still needs
    the previous run's card.
    """
    if not qn:
        return []
    try:
        rows = conn.execute("SELECT id, slug, qualified_name, links_json, description FROM declared_node "
                            "WHERE project = ? AND state = 'retired' AND superseded_by IS NULL ORDER BY id",
                            (project,)).fetchall()
    except sqlite3.Error:
        return []                       # an orchestrator DB from before migration 023
    out: list[dict] = []
    for row in rows:
        try:
            links = json.loads(row["links_json"] or "[]")
        except ValueError:
            continue
        if not any(isinstance(d, dict) and d.get("to") == qn for d in links):
            continue
        key = _node_key(psg, row["qualified_name"])
        last = _psg_rows(psg, "SELECT id, event_type, run_id FROM node_event WHERE node_key = ? "
                              "ORDER BY run_id DESC, seq DESC LIMIT 1", (key,)) if key else []
        if not (last and last[0][1] == "node_removed"):
            continue                    # retired in the ledger but the graph has not been refreshed yet
        out.append({"qualified_name": row["qualified_name"], "node_key": key,
                    "event_id": last[0][0], "run_id": last[0][2], "declared": True,
                    "declaration": row["description"]})
    return out


def _records(conn, project: str, anchors: list[str]) -> list[dict]:
    if not anchors:
        return []
    ph = ",".join("?" * len(anchors))
    return [dict(r) for r in conn.execute(
        f"SELECT r.id, r.node_key, r.plan_id, r.step_id, r.role, r.tier, r.evidence_level, r.rule_id, r.state, r.superseded_by, r.supersedes, "
        f"       r.recorded_by, r.significance_eff AS significance, r.occurred_at, r.statement, "
        f"       COALESCE(r.interpretation, r.statement, substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start)) AS text, "
        # The quoted span used to be reachable ONLY as this COALESCE's last
        # resort, so any record that also carried a paraphrase hid the words it
        # was quoting — and said nothing about having them. That is backwards for
        # a `stated` record, whose entire claim is that the user's own words say
        # it. `node declare --confirm` writes both, so the decider and the date a
        # declaration was confirmed with were invisible to every reader.
        f"       CASE WHEN r.verbatim_utterance_id IS NOT NULL AND (r.interpretation IS NOT NULL OR r.statement IS NOT NULL) "
        f"            THEN substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start) END AS quoted "
        f"FROM change_reason_v r LEFT JOIN utterance u ON u.id = r.verbatim_utterance_id "
        f"WHERE r.project = ? AND r.node_key IN ({ph}) AND {provenance.live('r')} ORDER BY r.id DESC", (project, *anchors))]


def _slim(r: dict) -> dict:
    """The record shape that travels in a pack — with the length of the cut said
    out loud (FL-154).

    The text has always been cut at CAP_TEXT, mid-word, with no ellipsis and no
    marker; `--all` lifts the record COUNT, never the record LENGTH. Measured on
    the live ledger, #1988 is 1358 characters and showed 240 of them: a reader
    saw 17% of a row and had nothing in front of it saying so, which is worse
    than showing nothing — a severed clause reads like a whole sentence. The
    bound stays (a pack is a bounded read by construction); the silence does
    not. `text_chars` is the true length and `text_cut` what is missing, so
    every renderer can point at `provledger record #<id>` for the rest."""
    text = r["text"] or ""
    return {"id": r["id"], "role": r["role"], "tier": r["tier"], "evidence_level": r["evidence_level"], "rule_id": r.get("rule_id"),
            "recorded_by": r["recorded_by"], "occurred_at": r["occurred_at"], "plan_id": r["plan_id"],
            "text": text[:CAP_TEXT], "text_chars": len(text), "text_cut": max(0, len(text) - CAP_TEXT),
            # The words the record quotes, when it has a paraphrase as well. Bounded
            # like `text` and, like `text`, saying how much was cut — a new field
            # that truncated in silence would be FL-154 a second time.
            **({"corrects": provenance.supersedes_ids(r["supersedes"])} if r.get("supersedes") else {}),
            **({"quoted": (r["quoted"] or "")[:CAP_TEXT],
                "quoted_chars": len(r["quoted"] or ""),
                "quoted_cut": max(0, len(r["quoted"] or "") - CAP_TEXT)} if r.get("quoted") else {})}


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
          step_id: str | None = None, record: bool = True, caps: dict | None = None) -> Pack:
    """The pack for `targets` (qualified names or nk_ keys). ONE read-only PSG
    connection for the whole build (H2). `record=False` writes nothing.

    `caps` overrides any of DEFAULT_CAPS for this build; NO_CAP means the caller
    asked for all of that kind, and then nothing of it is counted as truncated
    and the budget will not fold it away either."""
    psg_path = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    psg = None
    if psg_path:
        try:
            psg = psg_bridge.open_ro(psg_path)
        except sqlite3.Error:
            psg = None
    pack = Pack(project=project, moment=moment, budget_tokens=budget_tokens, generated_at=_now(),
                caps={**DEFAULT_CAPS, **(caps or {})})
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
            # An `unstated` row is a SLOT, not a reason: it has no words by
            # construction (the table's CHECK forbids them). Counting it among
            # `reasons` made one output carry two definitions of the word — the
            # footer said "3 more reasons not expanded" where `--all` then showed
            # 5 of 5, the third being the slot already reported as `pending 1`.
            # It gets its own count, so the remainder the footer states is the
            # remainder the flag reveals.
            rea = [r for r in recs if r["role"] == "reason" and r["tier"] != "unstated"]
            unstated = [r for r in recs if r["role"] == "reason" and r["tier"] == "unstated"]
            minor = [r for r in rea if r.get("significance") == "minor"]
            rea = [r for r in rea if r.get("significance") != "minor"]
            if _uncapped(pack, "reasons"):
                # "every reason" means every reason: a lifted cap unfolds the ones
                # an explicit `minor` significance had folded into a count, too.
                rea = sorted(rea + minor, key=lambda r: r["id"], reverse=True)
                minor = []
            tp.counts = {"constraints": len(cons), "rejected_paths": len(rej), "reasons": len(rea) + len(minor),
                         "reasons_minor": len(minor), "unstated": len(unstated)}
            tp.constraints = [_slim(r) for r in cons[:pack.caps["constraints"]]]
            tp.rejected_paths = [_slim(r) for r in rej[:pack.caps["rejected_paths"]]]
            tp.reasons = [_slim(r) for r in rea[:pack.caps["reasons"]]]
            tp.prior_outcomes = _prior_outcomes(conn, project, tp.identity_chain)
            card = _card(psg, qn)
            all_callers = list(card.get("callers") or [])
            tp.callers = all_callers[:pack.caps["callers"]]
            tp.counts["callers"] = len(all_callers)
            if len(all_callers) > len(tp.callers):
                pack.truncated["callers"] = pack.truncated.get("callers", 0) + len(all_callers) - len(tp.callers)
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
            tp.removed_upstream.extend(_retired_declarations(conn, psg, project, qn))
            for nb in tp.output_consumers:
                if nb in pack.neighbors_counts:
                    continue
                nk = _node_key(psg, nb)
                nrecs = _records(conn, project, [x for x in (nk, nb) if x]) if (nk or nb) else []
                ncons = [r for r in nrecs if r["role"] == "constraint" and r["state"] == "active" and r["superseded_by"] is None]
                entry = {"constraints": len(ncons), "rejected_paths": sum(1 for r in nrecs if r["role"] == "rejected_path")}
                if neighbors == "constraints" and ncons:
                    entry["statements"] = [_slim(r) for r in ncons[:pack.caps["neighbor_constraints"]]]
                    if len(ncons) > len(entry["statements"]):
                        pack.truncated["neighbor_constraints"] = pack.truncated.get("neighbor_constraints", 0) + len(ncons) - len(entry["statements"])
                pack.neighbors_counts[nb] = entry
            # caps beyond the fixed per-node limits count as truncated too
            if len(rej) > len(tp.rejected_paths):
                pack.truncated["rejected_paths"] = pack.truncated.get("rejected_paths", 0) + len(rej) - len(tp.rejected_paths)
            if len(rea) > len(tp.reasons):
                pack.truncated["reasons"] = pack.truncated.get("reasons", 0) + len(rea) - len(tp.reasons)
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


FOLD_CALLERS = 3          # what stays of the callers list once structure has to fold


def _uncapped(pack: Pack, kind: str) -> bool:
    """True when the caller lifted this kind's cap. Neither the record trim nor
    the structure fold may touch such a kind: the reader named it, and a bound
    they cannot see is exactly the defect `caps` exists to remove."""
    cap = pack.caps.get(kind)
    return cap is not None and cap >= NO_CAP


def _fold_structure(pack: Pack) -> bool:
    """When the record trim (which never cuts a kind to zero) cannot reach the
    budget, the structure folds into counts: callers down to FOLD_CALLERS,
    lineage_downstream and dtype_map to their sizes — every fold counted in
    `truncated` so the hint can say what `--impact` would expand. A kind whose
    cap the caller lifted is left alone. Returns True when something folded."""
    folded = False
    for tp in pack.targets:
        if len(tp.callers) > FOLD_CALLERS and not _uncapped(pack, "callers"):
            pack.truncated["callers"] = pack.truncated.get("callers", 0) + len(tp.callers) - FOLD_CALLERS
            tp.callers = tp.callers[:FOLD_CALLERS]
            folded = True
        if tp.lineage_downstream and not _uncapped(pack, "lineage_downstream"):
            tp.counts["lineage_downstream"] = len(tp.lineage_downstream)
            pack.truncated["lineage_downstream"] = pack.truncated.get("lineage_downstream", 0) + len(tp.lineage_downstream)
            tp.lineage_downstream = []
            folded = True
        if tp.dtype_map and not _uncapped(pack, "dtype_map"):
            tp.counts["dtype_map"] = len(tp.dtype_map)
            pack.truncated["dtype_map"] = pack.truncated.get("dtype_map", 0) + len(tp.dtype_map)
            tp.dtype_map = {}
            folded = True
    return folded


def _trim_to_budget(pack: Pack) -> None:
    """Drop records in TRIM_ORDER until the pack fits; count each drop. When the
    records are at their floor and the pack is still over, fold the structure."""
    def over() -> bool:
        return _tokens(pack.as_dict()) > pack.budget_tokens

    for kind in TRIM_ORDER:
        if _uncapped(pack, kind):
            continue
        while over():
            dropped = False
            if kind == "neighbor_constraints":
                for entry in pack.neighbors_counts.values():
                    if entry.get("statements"):
                        entry["statements"].pop()
                        dropped = True
                        break
            else:
                # never cut a kind to zero on a target: the structure alone (cards, identity
                # chains) can exceed the budget, and then at least one record of each kind
                # still reaches the reader — approx_tokens reports the overrun honestly
                for tp in reversed(pack.targets):
                    lst = getattr(tp, kind)
                    if len(lst) > 1:
                        lst.pop()
                        dropped = True
                        break
            if not dropped:
                break
            pack.truncated[kind] = pack.truncated.get(kind, 0) + 1
    if over():
        _fold_structure(pack)



def _hints(pack: Pack) -> list[str]:
    out = []
    labels = {"reasons": "reasons", "rejected_paths": "rejected paths", "neighbor_constraints": "neighbour constraints",
              "constraints": "constraints", "reasons_minor": "minor reasons"}
    attr = {"reasons": "reasons", "rejected_paths": "rejected_paths", "constraints": "constraints", "reasons_minor": "reasons"}

    def cut_target(kind: str) -> str:
        """The first target whose records of `kind` were cut — the one `--all` should expand."""
        for tp in pack.targets:
            have = len(getattr(tp, attr.get(kind, "reasons"), []))
            if tp.counts.get(kind, tp.counts.get("reasons", 0) if kind == "reasons_minor" else 0) > have:
                return tp.qualified_name
        return pack.targets[0].qualified_name if pack.targets else "<node>"
    structure = {k: pack.truncated.get(k, 0) for k in ("callers", "lineage_downstream", "dtype_map") if pack.truncated.get(k)}
    for kind, n in pack.truncated.items():
        if n and kind not in structure:
            out.append(f"{n} more {labels.get(kind, kind)} not expanded — `provledger why {cut_target(kind)} --all`")
    if structure:
        first = pack.targets[0].qualified_name if pack.targets else "<node>"
        out.append("structure folded into counts: " + " · ".join(f"{k} {n}" for k, n in structure.items()) + f" (`provledger why {first} --impact` names them)")
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
