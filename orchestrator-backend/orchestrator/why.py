"""why — `provledger why <target>`: one bounded, self-describing read of a
node's history (DP phase 2, Task 4).

The same `context_pack.build` that publish uses answers the question, so
plan time and on-demand never disagree. Everything printed is a record that
was put in front of someone: each line writes `read_hit(moment='why')`.
The summary line is fixed so a person can read it at a glance:

    <node> · 下游 n · 履历 m 次 · 约束 k（生效 j）· 否决 r · 待补 p

and every record carries `#id · tier · 来源等级 · occurred_at · 展示 n 次 ·
被 <plan> 采用` — how often it was shown before, and which plan adopted it.
Trimmed records appear as counts ("还有 n 条 …"), never vanish.

`--search` uses an FTS5 index built lazily here (`ensure_fts`); when the
sqlite build has no FTS5 module it falls back to LIKE and says so on the
first line. `--never-read` lists the constraints nobody was ever shown (D3);
`--pending` the unstated slots. `export --md` writes one markdown per node
with the shareable rows only; `init --agents-md` drops the two verbs into a
repo's AGENTS.md.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

from . import context_pack, psg_bridge

LEVEL_CN = {"linked": "有链接", "verbal": "口头", "task_context": "任务上下文", "unstated": "未说明"}
ROLE_CN = {"constraint": "约束", "rejected_path": "否决", "reason": "理由"}
_FILE_LINE = re.compile(r"^(?P<file>[^\s:]+\.[A-Za-z0-9]+):(?P<line>\d+)$")
FTS_TABLE = "change_reason_fts"


# ── target resolution ────────────────────────────────────────────────────────

def nodes_at(psg_db_path: str | None, file_path: str, line_lo: int, line_hi: int) -> list[dict]:
    return psg_bridge.nodes_at(psg_db_path, file_path, line_lo, line_hi)


def resolve_target(psg_db_path: str | None, target: str) -> dict:
    """qualified name | nk_… | file:line → {qualified_name, node_key, how}."""
    t = (target or "").strip()
    if t.startswith("nk_"):
        qn = psg_bridge.latest_qualified_name(psg_db_path, t) or t
        return {"qualified_name": qn, "node_key": t, "how": "node_key"}
    m = _FILE_LINE.match(t)
    if m:
        line = int(m.group("line"))
        hits = nodes_at(psg_db_path, m.group("file"), line, line)
        if hits:
            return {"qualified_name": hits[0]["qualified_name"], "node_key": hits[0]["node_key"], "how": "file:line"}
        return {"qualified_name": t, "node_key": None, "how": "file:line"}
    return {"qualified_name": t, "node_key": psg_bridge.node_key_of(psg_db_path, t), "how": "qualified_name"}


# ── per-record stats: shown n times, adopted by <plan> ───────────────────────

def stats_for(conn, reason_ids) -> dict[int, dict]:
    ids = sorted({int(x) for x in reason_ids})
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    out = {i: {"shown": 0, "by_moment": {}, "adopted_by": []} for i in ids}
    for rid, moment, n in conn.execute(f"SELECT reason_id, moment, COUNT(*) FROM read_hit WHERE reason_id IN ({ph}) GROUP BY 1, 2", ids):
        out[rid]["shown"] += n
        out[rid]["by_moment"][moment] = n
    for rid, plan_id, session_id in conn.execute(f"SELECT DISTINCT reason_id, plan_id, session_id FROM influence WHERE reason_id IN ({ph}) ORDER BY id", ids):
        out[rid]["adopted_by"].append(plan_id or (f"session {session_id}" if session_id else "?"))
    return out


def _line(rec: dict, st: dict) -> str:
    adopted = ("被 " + ", ".join(st["adopted_by"]) + " 采用") if st.get("adopted_by") else "未被采用"
    text = (rec.get("text") or "").replace("\n", " ")
    return (f"#{rec['id']} · {rec['tier']} · {LEVEL_CN.get(rec.get('evidence_level'), rec.get('evidence_level') or '?')} · "
            f"{(rec.get('occurred_at') or '')[:16]} · 展示 {st.get('shown', 0)} 次 · {adopted}\n    {text}")


# ── FTS (lazy) ───────────────────────────────────────────────────────────────

def ensure_fts(conn) -> bool:
    """Create the external-content FTS5 index the first time; False when this
    sqlite has no FTS5 (the caller degrades to LIKE and says so)."""
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (FTS_TABLE,)).fetchone()
        if not exists:
            conn.executescript(f"""
                CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5(interpretation, statement, content='change_reason', content_rowid='id');
                INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES ('rebuild');
                CREATE TRIGGER IF NOT EXISTS trg_change_reason_fts_ai AFTER INSERT ON change_reason BEGIN
                    INSERT INTO {FTS_TABLE}(rowid, interpretation, statement) VALUES (new.id, new.interpretation, new.statement);
                END;
            """)
            conn.commit()
        return True
    except sqlite3.OperationalError:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return False


def _fts_query(conn, project: str, q: str, limit: int) -> list[dict]:
    rows = conn.execute(
        f"SELECT r.id, r.node_key, r.plan_id, r.role, r.tier, r.evidence_level, r.recorded_by, r.occurred_at, "
        f"       COALESCE(r.interpretation, r.statement) AS text "
        f"FROM {FTS_TABLE} f JOIN change_reason_v r ON r.id = f.rowid WHERE {FTS_TABLE} MATCH ? AND r.project = ? "
        f"ORDER BY rank LIMIT ?", (q, project, limit)).fetchall()
    return [dict(zip(("id", "node_key", "plan_id", "role", "tier", "evidence_level", "recorded_by", "occurred_at", "text"), r)) for r in rows]


def _like_query(conn, project: str, q: str, limit: int) -> list[dict]:
    like = f"%{q}%"
    rows = conn.execute(
        "SELECT r.id, r.node_key, r.plan_id, r.role, r.tier, r.evidence_level, r.recorded_by, r.occurred_at, "
        "       COALESCE(r.interpretation, r.statement) AS text "
        "FROM change_reason_v r WHERE r.project = ? AND (r.interpretation LIKE ? OR r.statement LIKE ?) "
        "ORDER BY r.id DESC LIMIT ?", (project, like, like, limit)).fetchall()
    return [dict(zip(("id", "node_key", "plan_id", "role", "tier", "evidence_level", "recorded_by", "occurred_at", "text"), r)) for r in rows]


def search(conn, *, project: str, query: str, limit: int = 20) -> tuple[list[dict], bool]:
    """(rows, degraded): FTS5 when available, LIKE otherwise."""
    if ensure_fts(conn):
        try:
            return _fts_query(conn, project, query, limit), False
        except sqlite3.OperationalError:
            pass
    return _like_query(conn, project, query, limit), True


# ── the lists: --pending / --never-read ──────────────────────────────────────

def pending(conn, *, project: str, anchors: list[str] | None = None, limit: int = 50) -> list[dict]:
    """Unstated slots (the gaps): of the target when anchors are given, else of the project."""
    sql = ("SELECT id, node_key, plan_id, role, tier, evidence_level, recorded_by, occurred_at, NULL AS text "
           "FROM change_reason_v WHERE project = ? AND tier = 'unstated'")
    params: list = [project]
    if anchors:
        sql += f" AND node_key IN ({','.join('?' * len(anchors))})"
        params += anchors
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
    return [dict(zip(("id", "node_key", "plan_id", "role", "tier", "evidence_level", "recorded_by", "occurred_at", "text"), r)) for r in rows]


def never_read(conn, *, project: str, limit: int = 50) -> list[dict]:
    """Active constraints with no read_hit at all (D3): recorded, never shown to anyone."""
    rows = conn.execute(
        "SELECT r.id, r.node_key, r.plan_id, r.role, r.tier, r.evidence_level, r.recorded_by, r.occurred_at, r.statement AS text "
        "FROM change_reason_v r WHERE r.project = ? AND r.role = 'constraint' AND r.state = 'active' AND r.superseded_by IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM read_hit h WHERE h.reason_id = r.id) ORDER BY r.id DESC LIMIT ?", (project, limit)).fetchall()
    return [dict(zip(("id", "node_key", "plan_id", "role", "tier", "evidence_level", "recorded_by", "occurred_at", "text"), r)) for r in rows]


# ── the main read ────────────────────────────────────────────────────────────

def why(conn, *, project: str, target: str | None = None, psg_db_path: str | None = None, impact: bool = False,
        neighbors: bool = False, budget: int = 1500, fmt: str = "md", pending_only: bool = False,
        never_read_only: bool = False, all_records: bool = False, search_query: str | None = None,
        session_id: str | None = None, plan_id: str | None = None, record: bool = True) -> dict:
    """Returns {"text": <md>, "doc": <json-able>}; writes read_hit(moment='why') for every record shown."""
    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    shown: list[int] = []
    lines: list[str] = []
    doc: dict = {"project": project, "target": target, "records": []}

    if search_query:
        rows, degraded = search(conn, project=project, query=search_query, limit=20)
        doc.update({"mode": "search", "query": search_query, "degraded": degraded, "records": rows})
        if degraded:
            lines.append("（全文索引不可用，本次用 LIKE 匹配）")
        lines.append(f"search {search_query!r} · {len(rows)} 条")
        st = stats_for(conn, [r["id"] for r in rows])
        for r in rows:
            lines.append(f" {ROLE_CN.get(r['role'], r['role'])} {r['node_key'] or '-'} · " + _line(r, st.get(r["id"], {})))
        shown = [r["id"] for r in rows]
    elif never_read_only:
        rows = never_read(conn, project=project)
        doc.update({"mode": "never-read", "records": rows})
        lines.append(f"从未展示过的生效约束 · {len(rows)} 条")
        st = stats_for(conn, [r["id"] for r in rows])
        for r in rows:
            lines.append(f" {r['node_key'] or '-'} · " + _line(r, st.get(r["id"], {})))
        shown = [r["id"] for r in rows]
    elif pending_only and not target:
        rows = pending(conn, project=project)
        doc.update({"mode": "pending", "records": rows})
        lines.append(f"待补的理由（unstated）· {len(rows)} 条")
        for r in rows:
            lines.append(f" #{r['id']} · {r['node_key'] or '-'} · {r['plan_id']} · {(r['occurred_at'] or '')[:16]}")
        shown = []                                   # an unstated row has nothing to show
    else:
        if not target:
            raise ValueError("why needs a target (qualified name, nk_… or file:line) unless --search / --pending / --never-read")
        res = resolve_target(psg, target)
        doc["resolved"] = res
        pack = context_pack.build(conn, project=project, targets=[res["node_key"] or res["qualified_name"]], psg_db_path=psg,
                                  budget_tokens=(10 ** 9 if all_records else budget), neighbors="constraints" if neighbors else "counts",
                                  moment="why", session_id=session_id, plan_id=plan_id, record=False)
        tp = pack.targets[0] if pack.targets else None
        if tp is None:
            lines.append(f"{target}: 图里没有这个节点，账本里也没有记录")
            doc["records"] = []
        else:
            anchors = ([tp.node_key] if tp.node_key else []) + [n for n in tp.identity_chain if n]
            allrecs = context_pack._records(conn, project, anchors)
            cons_all = [r for r in allrecs if r["role"] == "constraint"]
            cons_active = [r for r in cons_all if r["state"] == "active" and r["superseded_by"] is None]
            rej_all = [r for r in allrecs if r["role"] == "rejected_path"]
            unst = [r for r in allrecs if r["tier"] == "unstated"]
            history = 0
            if tp.node_key:
                h = psg_bridge._query(psg, "SELECT COUNT(*) FROM node_event WHERE node_key = ?", (tp.node_key,))
                history = int(h[0][0]) if h else 0
            summary = (f"{tp.qualified_name} · 下游 {len(tp.output_consumers) + len(tp.lineage_downstream)} · 履历 {history} 次 · "
                       f"约束 {len(cons_all)}（生效 {len(cons_active)}）· 否决 {len(rej_all)} · 待补 {len(unst)}")
            lines.append(summary)
            doc["summary"] = {"qualified_name": tp.qualified_name, "node_key": tp.node_key, "downstream": len(tp.output_consumers) + len(tp.lineage_downstream),
                              "history": history, "constraints": len(cons_all), "constraints_active": len(cons_active),
                              "rejected_paths": len(rej_all), "pending": len(unst), "identity_chain": tp.identity_chain}
            if pending_only:
                recs = {"待补": [dict(r, text=None) for r in unst]}
            elif all_records:
                recs = {"约束": [context_pack._slim(r) for r in cons_active],
                        "否决": [context_pack._slim(r) for r in rej_all],
                        "理由": [context_pack._slim(r) for r in allrecs if r["role"] == "reason" and r["tier"] != "unstated"]}
            else:
                recs = {"约束": tp.constraints, "否决": tp.rejected_paths, "理由": tp.reasons}
            for label, items in recs.items():
                if not items:
                    continue
                lines.append(f"── {label} · {len(items)}")
                st = stats_for(conn, [r["id"] for r in items])
                for r in items:
                    if r.get("tier") == "unstated":
                        lines.append(f" #{r['id']} · unstated · {r['plan_id']} · {(r['occurred_at'] or '')[:16]}")
                        continue
                    lines.append(" " + _line(r, st.get(r["id"], {})))
                    shown.append(r["id"])
                    doc["records"].append({**r, **st.get(r["id"], {}), "layer": label})
            if tp.prior_outcomes and not pending_only:
                lines.append(f"── 过往承诺 · {len(tp.prior_outcomes)}")
                for o in tp.prior_outcomes:
                    lines.append(f" {o['plan_id']} · \"{o['claim']}\" · {o['kind']} {o.get('signal') or o.get('delta_pct') or ''}")
            if impact or neighbors:
                lines.append(f"── 影响面 · 调用方 {len(tp.callers)} · 下游消费 {len(tp.output_consumers)} · 血缘下游 {len(tp.lineage_downstream)}")
                for nb in tp.output_consumers[:8]:
                    entry = pack.neighbors_counts.get(nb) or {}
                    lines.append(f"   {nb} · 约束 {entry.get('constraints', 0)} · 否决 {entry.get('rejected_paths', 0)}")
                    for s in entry.get("statements") or []:
                        st = stats_for(conn, [s["id"]])
                        lines.append("     " + _line(s, st.get(s["id"], {})))
                        shown.append(s["id"])
                        doc["records"].append({**s, **st.get(s["id"], {}), "layer": f"downstream:{nb}"})
            else:
                lines.append(f"── 影响面：调用方 {len(tp.callers)} · 下游消费 {len(tp.output_consumers)}（`--impact` 展开）")
            for h in pack.hints:
                lines.append(" " + h)
            doc["hints"] = list(pack.hints)
            doc["truncated"] = dict(pack.truncated)
    if record and shown:
        context_pack.write_read_hits(conn, project=project, reason_ids=sorted(set(shown)), moment="why", plan_id=plan_id,
                                     session_id=session_id, step_id=None)
    doc["shown"] = len(set(shown))
    return {"text": "\n".join(lines), "doc": doc}


# ── export --md ──────────────────────────────────────────────────────────────

def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", s)[:120] or "node"


def export_md(conn, *, project: str, out_dir: str, psg_db_path: str | None = None) -> dict:
    """One markdown per node: shareable reasons / constraints / rejected paths,
    their linked references (label + uri), and a history line. Rows whose
    statement is personal never leave the database; a personal rationale is
    omitted from a shareable row (E1, first half).

    DP phase 3: a shareable record may quote words that are not shareable. The
    record travels, the quotation does not, and the file says which utterance
    was withheld instead of leaving a blank where a sentence used to be."""
    psg = psg_db_path if psg_db_path is not None else psg_bridge.db_path_for(project)
    os.makedirs(out_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT r.id, r.node_key, r.plan_id, r.role, r.tier, r.evidence_level, r.recorded_by, r.occurred_at, r.state, "
        "       r.rationale, r.rationale_visibility, "
        "       COALESCE(r.interpretation, r.statement, "
        "               CASE WHEN u.visibility = 'shareable' "
        "                    THEN substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start) END) AS text, "
        "       CASE WHEN r.verbatim_utterance_id IS NOT NULL AND r.interpretation IS NULL AND r.statement IS NULL "
        "                 AND COALESCE(u.visibility, 'personal') <> 'shareable' "
        "            THEN r.verbatim_utterance_id END AS withheld_utterance "
        "FROM change_reason_v r LEFT JOIN utterance u ON u.id = r.verbatim_utterance_id "
        "WHERE r.project = ? AND r.node_key IS NOT NULL AND r.statement_visibility = 'shareable' ORDER BY r.node_key, r.id", (project,)).fetchall()
    by_node: dict[str, list] = {}
    for r in rows:
        by_node.setdefault(r[1], []).append(r)
    files = []
    for key, recs in by_node.items():
        qn = psg_bridge.latest_qualified_name(psg, key) or key
        events = psg_bridge._query(psg, "SELECT COUNT(*) FROM node_event WHERE node_key = ?", (key,)) if psg else []
        n_events = int(events[0][0]) if events else 0
        out = [f"# {qn}", "", f"- node_key: `{key}`", f"- 履历：{n_events} 次事件", f"- 记录：{len(recs)} 条（只含可共享的）", ""]
        for label, role in (("约束", "constraint"), ("理由", "reason"), ("否决", "rejected_path")):
            items = [r for r in recs if r[3] == role]
            if not items:
                continue
            out.append(f"## {label}")
            for r in items:
                rid, _, plan_id, _, tier, level, by, at, state, rationale, rvis, text, withheld = r
                out.append(f"- #{rid} · {tier} · 来源等级 {LEVEL_CN.get(level, level)} · {(at or '')[:10]} · {by} · {plan_id}" + (f" · {state}" if state != "active" else ""))
                if text:
                    out.append(f"  - {text}")
                elif withheld is not None:
                    out.append(f"  - （引自 utterance #{withheld}，标为 personal —— 原话不外发）")
                if rationale and rvis == "shareable":
                    out.append(f"  - 为什么：{rationale}")
                for kind, label_, uri in conn.execute(
                        "SELECT f.kind, f.label, f.uri FROM reference_link l JOIN reference f ON f.id = l.reference_id "
                        "WHERE l.reason_id = ? AND f.visibility = 'shareable' ORDER BY f.id", (rid,)):
                    out.append(f"  - 来源：{kind} · {label_}" + (f" · {uri}" if uri else ""))
            out.append("")
        path = os.path.join(out_dir, _safe_name(qn) + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
        files.append(path)
    return {"project": project, "nodes": len(by_node), "files": files, "rows": len(rows)}


# ── init --agents-md ─────────────────────────────────────────────────────────

AGENTS_BEGIN = "<!-- provledger:begin -->"
AGENTS_END = "<!-- provledger:end -->"
AGENTS_SNIPPET = f"""{AGENTS_BEGIN}
## provledger（决策溯源）

两个动词就够：

- `provledger why <node|nk_…|file:line>` — 改一个东西之前先问一句：它的约束、被否决过的路子、过去的理由，一次拿完、有上界；`--impact` 看影响面，`--all` 展开裁掉的，`--search "<词>"` 全文找。
- `provledger note "<原话>" --at "<时间>" [--node <qn>]` — 有人当面/邮件里定了什么，事后记进来，原话原样存。

什么时候调：plan 发布时 headline 里出现 `⚠ blocking` 的发现；PreToolUse 提示"该处有约束"；关闭 plan 填理由时想引用旧记录（写 `because: [id]`）。

怎么回答 headline：每条 blocking 发现用 `headline-respond`（`action: revise | proceed` + 一句 rationale + 引用的记录 id）；不回答也不会被拦，但会被记成"未回答"并在关闭时留下一条承诺。

措辞：来源等级 / 展示过 / 采用过。
{AGENTS_END}
"""


def init_agents_md(cwd: str) -> dict:
    """Write or refresh the provledger block in <cwd>/AGENTS.md (idempotent)."""
    path = os.path.join(cwd, "AGENTS.md")
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    if AGENTS_BEGIN in existing and AGENTS_END in existing:
        head, _, rest = existing.partition(AGENTS_BEGIN)
        _, _, tail = rest.partition(AGENTS_END)
        new = head + AGENTS_SNIPPET.rstrip("\n") + tail
        action = "replaced"
    elif existing:
        new = existing.rstrip("\n") + "\n\n" + AGENTS_SNIPPET
        action = "appended"
    else:
        new = AGENTS_SNIPPET
        action = "created"
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    return {"path": path, "action": action}
