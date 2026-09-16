"""ask.facts — the fact table (DP phase 2e, Task 1; spec §21).

Everything the answer is allowed to say, computed by queries alone. Per chosen
node: the active constraints with their sources, how often each was shown and
which plans adopted it; the reasons and the rejected paths; the influence rows
(which plan changed because of which record); the graph's change history
(`node_matched` is not a change); the expectations and their outcomes — with
"none" spelled out; the measured values; and the day the node was first seen
and last changed.

Every fact carries a **cite**, and the cite namespace is the contract with the
model: `#12` a ledger record, `#r3` a source, `#i4` an influence row, `#e5` a
change event, `#x6` an expectation, `#o7` an outcome, `#m8` a measured value.
`render()` is the exact text the model is given; `numbers_in(render(ft))` is
the set of numbers the answer may contain (J2); `sha()` stamps the table into
`ask_log.facts_sha` so an answer can be replayed against what it saw.

This module writes nothing — not even a read_hit: a question is not a plan.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from .. import context_pack, psg_bridge

CAP = {"constraints": 20, "reasons": 10, "rejected_paths": 10, "influence": 20, "changes": 20,
       "expectations": 10, "values": 20}
NOT_A_CHANGE = ("node_matched",)
_CITE = re.compile(r"\[#([A-Za-z]{0,2}\d+)\]")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cites_in(text: str) -> set[str]:
    """`{"#12", "#i4"}` — every cite token in a rendering or an answer."""
    return {"#" + m.group(1) for m in _CITE.finditer(text or "")}


def numbers_in(text: str) -> set[str]:
    return {m.group(0) for m in _NUMBER.finditer(text or "")}


# ── per-record decoration ────────────────────────────────────────────────────

def _references(conn, reason_id: int) -> list[dict]:
    return [{"cite": f"#r{r[0]}", "kind": r[1], "label": r[2], "uri": r[3]} for r in conn.execute(
        "SELECT f.id, f.kind, f.label, f.uri FROM reference_link l JOIN reference f ON f.id = l.reference_id "
        "WHERE l.reason_id = ? ORDER BY f.id", (reason_id,))]


def _shown_and_adopted(conn, reason_ids: list[int]) -> dict[int, dict]:
    if not reason_ids:
        return {}
    ph = ",".join("?" * len(reason_ids))
    out = {i: {"shown": 0, "adopted_by": []} for i in reason_ids}
    for rid, n in conn.execute(f"SELECT reason_id, COUNT(*) FROM read_hit WHERE reason_id IN ({ph}) GROUP BY 1", reason_ids):
        out[rid]["shown"] = n
    for rid, plan_id in conn.execute(
            f"SELECT DISTINCT reason_id, plan_id FROM influence WHERE reason_id IN ({ph}) ORDER BY reason_id, plan_id", reason_ids):
        if plan_id:
            out[rid]["adopted_by"].append(plan_id)
    return out


def _record(conn, r: dict, stats: dict) -> dict:
    st = stats.get(r["id"], {})
    return {"cite": f"#{r['id']}", "id": r["id"], "role": r["role"], "tier": r["tier"],
            "evidence_level": r.get("evidence_level"), "state": r.get("state"), "plan_id": r.get("plan_id"),
            "recorded_by": r.get("recorded_by"), "occurred_at": r.get("occurred_at"),
            "text": (r.get("text") or "").replace("\n", " ").strip(),
            "shown": st.get("shown", 0), "adopted_by": st.get("adopted_by", []),
            "references": _references(conn, r["id"])}


# ── per-node queries ─────────────────────────────────────────────────────────

def _changes(psg_db_path: str | None, node_key: str | None, cap: int) -> tuple[list[dict], str | None, str | None]:
    """(significant events, first_seen, last_changed) — dates, not timestamps."""
    if not node_key:
        return [], None, None
    rows = psg_bridge._query(psg_db_path,
                             "SELECT id, event_type, run_id, created_at, tier FROM node_event WHERE node_key = ? ORDER BY run_id, seq",
                             (node_key,))
    seen = [r[3] for r in rows if r[3]]
    sig = [{"cite": f"#e{r[0]}", "id": r[0], "event_type": r[1], "run_id": r[2],
            "at": (r[3] or "")[:10], "tier": r[4]} for r in rows if r[1] not in NOT_A_CHANGE]
    first = min(seen)[:10] if seen else None
    last = max((c["at"] for c in sig if c["at"]), default=None)
    return sig[-cap:], first, last


def _influence(conn, project: str, anchors: list[str], reason_ids: list[int], cap: int) -> list[dict]:
    where = []
    params: list = [project]
    if anchors:
        where.append(f"node_key IN ({','.join('?' * len(anchors))})")
        params += anchors
    if reason_ids:
        where.append(f"reason_id IN ({','.join('?' * len(reason_ids))})")
        params += [int(i) for i in reason_ids]
    if not where:
        return []
    rows = conn.execute(f"SELECT id, reason_id, plan_id, session_id, via, by, at FROM influence "
                        f"WHERE project = ? AND ({' OR '.join(where)}) ORDER BY id", params).fetchall()
    return [{"cite": f"#i{r[0]}", "id": r[0], "reason_id": r[1], "plan_id": r[2] or (f"session {r[3]}" if r[3] else None),
             "via": r[4], "by": r[5], "at": r[6]} for r in rows][:cap]


def _expectations(conn, project: str, names: list[str], cap: int) -> list[dict]:
    if not names:
        return []
    ph = ",".join("?" * len(names))
    rows = conn.execute(f"SELECT id, plan_id, claim, channel, created_at FROM expectations "
                        f"WHERE project = ? AND target IN ({ph}) ORDER BY id", (project, *names)).fetchall()
    out = []
    for eid, plan_id, claim, channel, created_at in rows[:cap]:
        o = conn.execute("SELECT id, kind, tier, value_json, observed_at, reason FROM outcomes WHERE expectation_id = ? "
                         "ORDER BY observed_at DESC, id DESC LIMIT 1", (eid,)).fetchone()
        outcome = None
        if o is not None:
            try:
                value = json.loads(o[3]) if o[3] else {}
            except ValueError:
                value = {}
            outcome = {"cite": f"#o{o[0]}", "id": o[0], "kind": o[1], "tier": o[2], "value": value,
                       "observed_at": o[4], "reason": o[5]}
        out.append({"cite": f"#x{eid}", "id": eid, "plan_id": plan_id, "claim": claim, "channel": channel,
                    "created_at": created_at, "outcome": outcome})
    return out


def _metric_names(qn: str, expectations: list[dict]) -> list[str]:
    names = [qn.rsplit(".", 1)[-1]] if qn else []
    for e in expectations:
        ch = e.get("channel") or ""
        if ch.startswith("metric:"):
            n = ch.split("metric:", 1)[1].strip()
            if n and n not in names:
                names.append(n)
    return names


def _values(conn, project: str, names: list[str], cap: int) -> list[dict]:
    if not names:
        return []
    ph = ",".join("?" * len(names))
    rows = conn.execute(f"SELECT id, name, value, unit, observed_at, plan_id FROM metrics "
                        f"WHERE project = ? AND name IN ({ph}) ORDER BY observed_at, id", (project, *names)).fetchall()
    return [{"cite": f"#m{r[0]}", "id": r[0], "name": r[1], "value": r[2], "unit": r[3], "observed_at": r[4],
             "plan_id": r[5]} for r in rows][:cap]


# ── the table ────────────────────────────────────────────────────────────────

def facts(conn, psg_db_path: str | None, chosen: list[str], *, project: str) -> dict:
    """The fact table for the chosen nodes. Reads only."""
    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    pcon = None
    if psg:
        try:
            pcon = psg_bridge.open_ro(psg)
        except sqlite3.Error:
            pcon = None
    ft: dict = {"project": project, "generated_at": _now(), "nodes": [], "ids": {}, "numbers": set(),
                "counts": {}, "truncated": {}}
    try:
        for target in chosen:
            if not target:
                continue
            if str(target).startswith("nk_"):
                key = target
                qn = psg_bridge.latest_qualified_name(psg, key) or target
            else:
                qn, key = target, context_pack._node_key(pcon, target)
            chain = context_pack._identity_chain(pcon, key, qn)
            anchors = ([key] if key else []) + chain
            recs = context_pack._records(conn, project, anchors)
            stats = _shown_and_adopted(conn, [r["id"] for r in recs])
            cons = [r for r in recs if r["role"] == "constraint" and r["state"] == "active" and r["superseded_by"] is None]
            rej = [r for r in recs if r["role"] == "rejected_path"]
            rea = [r for r in recs if r["role"] == "reason" and r["tier"] != "unstated"]
            node: dict = {"qn": qn, "node_key": key, "status": "existing" if key else "new", "identity_chain": chain}
            for name, rows in (("constraints", cons), ("reasons", rea), ("rejected_paths", rej)):
                kept = list(reversed(rows))[-CAP[name]:] if len(rows) > CAP[name] else list(reversed(rows))
                if len(rows) > CAP[name]:
                    ft["truncated"][name] = ft["truncated"].get(name, 0) + len(rows) - CAP[name]
                node[name] = [_record(conn, r, stats) for r in kept]
            node["influence"] = _influence(conn, project, anchors, [r["id"] for r in recs], CAP["influence"])
            node["changes"], node["first_seen"], node["last_changed"] = _changes(psg, key, CAP["changes"])
            node["expectations"] = _expectations(conn, project, chain or [qn], CAP["expectations"])
            node["values"] = _values(conn, project, _metric_names(qn, node["expectations"]), CAP["values"])
            ft["nodes"].append(node)
    finally:
        if pcon is not None:
            pcon.close()

    index: dict = {}
    for node in ft["nodes"]:
        for name in ("constraints", "reasons", "rejected_paths"):
            for r in node[name]:
                index[r["cite"]] = {"kind": name, "node": node["qn"], **r}
                for ref in r["references"]:
                    # the cite namespace wins: a source's own kind (doc, meeting, …) is `source_kind`
                    index[ref["cite"]] = {**ref, "kind": "reference", "source_kind": ref["kind"], "node": node["qn"]}
        for r in node["influence"]:
            index[r["cite"]] = {"kind": "influence", "node": node["qn"], **r}
        for r in node["changes"]:
            index[r["cite"]] = {"kind": "change", "node": node["qn"], **r}
        for r in node["expectations"]:
            index[r["cite"]] = {"kind": "expectation", "node": node["qn"], **r}
            if r["outcome"]:
                index[r["outcome"]["cite"]] = {"kind": "outcome", "node": node["qn"], **r["outcome"]}
        for r in node["values"]:
            index[r["cite"]] = {"kind": "value", "node": node["qn"], **r}
    ft["ids"] = index
    ft["counts"] = {"nodes": len(ft["nodes"]),
                    **{k: sum(len(n[k]) for n in ft["nodes"]) for k in
                       ("constraints", "reasons", "rejected_paths", "influence", "changes", "expectations", "values")}}
    ft["numbers"] = numbers_in(render(ft))
    return ft


# ── rendering: the exact text the model is given ─────────────────────────────

def _fmt_record(r: dict, indent: str = "  ") -> list[str]:
    head = f"{indent}[{r['cite']}] {r['tier']} · {r.get('evidence_level') or '?'} · {(r.get('occurred_at') or '')[:10]}"
    if r.get("plan_id"):
        head += f" · plan {r['plan_id']}"
    head += f" · shown {r.get('shown', 0)}"
    head += (" · adopted by " + ", ".join(r["adopted_by"])) if r.get("adopted_by") else " · not adopted"
    out = [head]
    if r.get("text"):
        out.append(f"{indent}    {r['text']}")
    for ref in r.get("references") or ():
        out.append(f"{indent}    source [{ref['cite']}] {ref['kind']} · {ref['label']}" + (f" · {ref['uri']}" if ref.get("uri") else ""))
    return out


def render(ft: dict) -> str:
    """The fact table as text — the model's only material, and the reader's fallback."""
    lines = [f"Fact table · project {ft['project']} · {len(ft['nodes'])} node(s)"]
    for n in ft["nodes"]:
        lines.append("")
        lines.append(f"## {n['qn']} ({n['node_key'] or 'not in graph'}, {n['status']})")
        seen = f"first seen {n['first_seen']}" if n.get("first_seen") else "first seen: no record"
        changed = f"last changed {n['last_changed']}" if n.get("last_changed") else "last changed: no record"
        lines.append(f"{seen} · {changed}")
        for label, key in (("constraints", "constraints"), ("reasons", "reasons"), ("rejected paths", "rejected_paths")):
            lines.append(f"{label} ({len(n[key])})")
            for r in n[key]:
                lines += _fmt_record(r)
        lines.append(f"influence ({len(n['influence'])})")
        for r in n["influence"]:
            lines.append(f"  [{r['cite']}] plan {r['plan_id']} changed via {r['via']}, citing #{r['reason_id']}, by {r['by']}, {(r['at'] or '')[:10]}")
        lines.append(f"changes ({len(n['changes'])})")
        for r in n["changes"]:
            lines.append(f"  [{r['cite']}] {r['event_type']} · run {r['run_id']} · {r['at']}")
        lines.append(f"expectations ({len(n['expectations'])})")
        for r in n["expectations"]:
            o = r["outcome"]
            tail = (f"outcome [{o['cite']}] {o['kind']} · {o['tier']} · {json.dumps(o['value'], sort_keys=True)}"
                    if o else "outcome: none recorded")
            lines.append(f"  [{r['cite']}] plan {r['plan_id']} · \"{r['claim']}\" · channel {r['channel']} · {tail}")
        lines.append(f"values ({len(n['values'])})")
        for r in n["values"]:
            lines.append(f"  [{r['cite']}] {r['name']} = {r['value']}{(' ' + r['unit']) if r['unit'] else ''} · {(r['observed_at'] or '')[:10]}")
    if ft.get("truncated"):
        lines.append("")
        lines.append("truncated: " + " · ".join(f"{k} {v}" for k, v in sorted(ft["truncated"].items())))
    return "\n".join(lines)


def sha(ft: dict) -> str:
    """A stable sha256 of the table's content (not of the generation time)."""
    payload = {"project": ft["project"], "rendered": render(ft), "ids": sorted(ft["ids"])}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()
