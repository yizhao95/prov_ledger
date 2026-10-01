"""graph_view — `provledger graph`: the project's graph and its relations, in
context, folded (spec §10.2, §10.3).

§10.2 settled what a model needs in order to navigate a project on its own:
the current project's graph and its relations, plus one documented way to take
a node's history. Nothing else. No scored candidate list, no curated map
columns, no pre-assembled material — §10.1 measured two memoryless agents
answering correctly on the real ledger *by going around* the ranking layer,
which is why the ranking layer is gone rather than tuned.

§10.3 is then arithmetic, not taste. Measured on this repo's own graph: 18200
nodes, of which 15986 are not imported modules; those names ALONE cost ~239k
tokens (the spec's ~147k was measured over a narrower node set), and there are
68384 edges. A flat dump is impossible by a wide margin, so the
view must fold — and the whole point of this module is that **folding is not
selecting**:

    selecting  — deciding what is RELEVANT. That is the model's job, always,
                 and this module never does it: no scores, no ranking, no
                 `chosen`, no ordering by anything but size and name.
    folding    — not being able to hand over 147k tokens of names and 68k
                 edges at once. That is pagination, forced by arithmetic.

So every folded thing states its own size and the exact command that unfolds
it, and the depth is the reader's to choose. A row that says `with records 0`
is a row a model can skip without opening it, and the root view is the only
place that can say so cheaply — which is the one derived number here, and it is
a count of rows in the ledger, not a judgement about them.

**Imported modules are excluded by default.** The rule is one the graph itself
encodes rather than a blocklist of names: a node that is the destination of an
`imports` edge and has no `file_path` in this repo is a module the project
imports, not code the project owns. On the live graph that is exactly 2214 node
rows over 100 distinct names — `json` appears 202 times, `__future__` 195,
`pathlib` 168, `pytest` 165, `sqlite3` 154 — because the analyzer records one
module node per importing file. Grouping the unfiltered graph one level deep
yields 7205 groups, most of them this. `--include-imports` keeps them, and the
exclusion is always printed: a silent filter would be the same sin as a silent
ranking.

Read-only by construction: the graph is opened through
`psg_bridge.open_ro` (a `mode=ro` URI) and the ledger connection is only ever
SELECTed. No `read_hit`, no counter, nothing.
"""
from __future__ import annotations

import sqlite3

from . import psg_bridge

IMPORT_EDGE = "imports"
DEFAULT_LIMIT = 40              # names printed per group / per hop before folding
DEFAULT_DEPTH = 1
EXCLUDED_NAMES_SHOWN = 8        # of the distinct imported module names, how many to name
EXCLUSION_RULE = ("the destination of an `imports` edge with no file in this repo "
                  "(a module the project imports, not code the project owns)")


# ── reading the graph ────────────────────────────────────────────────────────

def _open(psg_db_path: str | None):
    """The graph, read-only — or None. A missing, unregistered or unreadable
    graph is a visible outcome here, never an exception (psg_bridge's rule)."""
    if not psg_db_path:
        return None
    try:
        return psg_bridge.open_ro(psg_db_path)
    except sqlite3.Error:
        return None


def _nodes(g, include_imports: bool) -> list[dict]:
    """Every node row of the graph, with the imported modules dropped unless
    asked for. `imported` is computed from the edge table, so the rule travels
    with the graph instead of living in a list of names here."""
    imported = set()
    if not include_imports:
        try:
            imported = {r[0] for r in g.execute(
                "SELECT DISTINCT e.dst_node_id FROM edge e JOIN edge_type t ON t.id = e.edge_type_id "
                "WHERE t.name = ? AND (SELECT n.file_path FROM node n WHERE n.id = e.dst_node_id) IS NULL",
                (IMPORT_EDGE,))}
        except sqlite3.OperationalError:
            imported = set()
    rows = g.execute("SELECT n.id, n.qualified_name, n.name, n.file_path, n.node_key, t.name "
                     "FROM node n JOIN node_type t ON t.id = n.node_type_id").fetchall()
    out = []
    for nid, qn, name, fp, key, ntype in rows:
        out.append({"id": nid, "qualified_name": qn or name, "file_path": fp, "node_key": key,
                    "node_type": ntype, "excluded": nid in imported})
    return out


def _edges(g) -> list[dict]:
    try:
        rows = g.execute("SELECT e.src_node_id, e.dst_node_id, t.name FROM edge e "
                         "JOIN edge_type t ON t.id = e.edge_type_id").fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"src": r[0], "dst": r[1], "edge_type": r[2]} for r in rows]


def _record_counts(ledger_conn, project: str | None) -> dict[str, int]:
    """node_key -> how many ledger records hang on it. Read-only, and the only
    number this module derives: it is a row count, not an opinion."""
    if ledger_conn is None or not project:
        return {}
    try:
        return {r[0]: r[1] for r in ledger_conn.execute(
            "SELECT node_key, COUNT(*) FROM change_reason WHERE project = ? AND node_key IS NOT NULL GROUP BY 1",
            (project,)) if r[0]}
    except sqlite3.Error:
        return {}


def _records_of(node: dict, counts: dict) -> int:
    return int(counts.get(node["node_key"] or "", 0))


def _type_counts(nodes) -> list[dict]:
    """What kinds of node are in here, biggest first.

    This exists because of what the live graph looks like: `api.py` holds 111
    `data_var` parameter nodes, 29 functions and 17 pipelines, so an
    alphabetical cut of 40 names is all parameters and the read cannot reach a
    single function. Dropping `data_var` would be SELECTION — deciding what is
    relevant — which §10.3 reserves for the model. Saying which kinds are
    present, and offering `--type` so the reader narrows it themselves, is
    folding. So the counts are printed and the choice is handed over."""
    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["node_type"]] = counts.get(n["node_type"], 0) + 1
    return [{"node_type": k, "nodes": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


# ── the root view: the graph folded to areas ─────────────────────────────────

def project_graph(psg_db_path: str | None, *, ledger_conn=None, project: str | None = None,
                  include_imports: bool = False) -> dict:
    """The whole graph folded to areas, small enough to sit in context.

    An area is `psg_bridge.subsystem_of` — the top two path segments, the same
    convention the graph page uses. On the live project that is 29 rows for
    15986 nodes, which is the point: the arithmetic in §10.3 rules out anything
    flatter, and 29 rows with their sizes let the reader pick the branch."""
    g = _open(psg_db_path)
    doc: dict = {"view": "areas", "graph": None, "project": project,
                 "totals": {"nodes": 0, "nodes_shown": 0, "nodes_excluded": 0, "with_records": 0,
                            "edges": 0, "edges_shown": 0, "edges_excluded": 0},
                 "relations": [], "areas": [],
                 "excluded": {"rule": EXCLUSION_RULE, "nodes": 0, "edges": 0, "names": [],
                              "include_with": "--include-imports"},
                 "folding": "folding is pagination, not selection: nothing here is ranked, scored or pre-picked"}
    if g is None:
        return doc
    doc["graph"] = psg_db_path
    try:
        nodes = _nodes(g, include_imports)
        edges = _edges(g)
    finally:
        g.close()
    counts = _record_counts(ledger_conn, project)
    shown = {n["id"]: n for n in nodes if not n["excluded"]}
    dropped = [n for n in nodes if n["excluded"]]
    by_area: dict[str, dict] = {}
    for n in shown.values():
        area = psg_bridge.subsystem_of(n["file_path"])
        row = by_area.setdefault(area, {"area": area, "nodes": 0, "with_records": 0})
        row["nodes"] += 1
        if _records_of(n, counts):
            row["with_records"] += 1
    for row in by_area.values():
        row["unfold"] = f"provledger graph {row['area']} --depth {DEFAULT_DEPTH}"
    doc["areas"] = sorted(by_area.values(), key=lambda r: (-r["nodes"], r["area"]))
    kinds: dict[str, int] = {}
    edges_shown = 0
    for e in edges:
        if e["src"] in shown and e["dst"] in shown:
            kinds[e["edge_type"]] = kinds.get(e["edge_type"], 0) + 1
            edges_shown += 1
    doc["relations"] = [{"edge_type": k, "edges": v} for k, v in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))]
    names: dict[str, int] = {}
    for n in dropped:
        names[n["qualified_name"]] = names.get(n["qualified_name"], 0) + 1
    doc["excluded"].update({
        "nodes": len(dropped), "edges": len(edges) - edges_shown,
        "names": [{"name": k, "nodes": v} for k, v in sorted(names.items(), key=lambda kv: (-kv[1], kv[0]))][:EXCLUDED_NAMES_SHOWN],
        "distinct_names": len(names)})
    doc["totals"] = {"nodes": len(nodes), "nodes_shown": len(shown), "nodes_excluded": len(dropped),
                     "with_records": sum(1 for n in shown.values() if _records_of(n, counts)),
                     "edges": len(edges), "edges_shown": edges_shown, "edges_excluded": len(edges) - edges_shown}
    return doc


# ── unfolding: a path, or a node's neighbourhood ─────────────────────────────

def _path_segments(p: str) -> list[str]:
    return [s for s in (p or "").replace("\\", "/").strip("/").split("/") if s]


def _under(node: dict, base: list[str]) -> bool:
    segs = _path_segments(node["file_path"] or "")
    return bool(segs) and segs[:len(base)] == base


def _area_members(nodes: list[dict], area: str) -> list[dict]:
    return [n for n in nodes if psg_bridge.subsystem_of(n["file_path"]) == area]


def _by_name(rows: list[dict], counts: dict) -> list[dict]:
    """One NAME is one line, with the number of graph node rows behind it.

    The analyzer records one node row per occurrence, so a name repeats: with
    `--include-imports` the `(external)` group printed `__future__` 195 times.
    Identical lines read as a bug and spend the budget once each, so the count
    moves onto the single line — the same rule the node view follows, because
    two halves of one command disagreeing about this would be its own defect."""
    out: dict[str, dict] = {}
    for n in rows:
        row = out.get(n["qualified_name"])
        if row is None:
            out[n["qualified_name"]] = {"qualified_name": n["qualified_name"], "node_type": n["node_type"],
                                        "node_key": n["node_key"], "records": _records_of(n, counts), "nodes": 1}
        else:
            row["nodes"] += 1
            row["records"] = max(row["records"], _records_of(n, counts))
            row["node_key"] = row["node_key"] or n["node_key"]
    return sorted(out.values(), key=lambda r: r["qualified_name"])


def _path_view(nodes: list[dict], counts: dict, target: str, depth: int, limit: int, *, as_area: bool = False,
               node_type: str | None = None) -> dict:
    """The nodes under a path (or in one area), folded to the next `depth` path
    levels.

    A group that IS a file is terminal, so its node names are printed (bounded
    by `limit`). A directory still deeper than the requested depth stays a
    count and carries the command that opens it — which is the whole contract:
    a number the reader can act on, never a number instead of a name.

    `as_area` selects by `subsystem_of` rather than by path prefix, and the two
    genuinely differ. Caught on the live graph: `orchestrator-backend` is an
    area of 17 nodes and a prefix of 8682, so the root view printed `nodes 17`
    beside a command that would have shown 8682 — the same contradiction as
    `callers 12` beside `callers 38`, which is the defect this whole change is
    about. A folded count and the command that unfolds it must be the same
    number. `subsystem_of` also invents `(external)` and `(root)`, which are not
    paths at all, so a member that does not sit under the target as a path lands
    in one terminal group named after the target instead of vanishing."""
    base = _path_segments(target)
    members = _area_members(nodes, target) if as_area else [n for n in nodes if _under(n, base)]
    if node_type:
        members = [n for n in members if n["node_type"] == node_type]
    groups: dict[str, dict] = {}
    for n in members:
        segs = _path_segments(n["file_path"] or "")
        if segs and segs[:len(base)] == base and len(segs) > len(base):
            level = min(depth, len(segs) - len(base))
            path = "/".join(segs[:len(base) + level])
            terminal = len(segs) == len(base) + level
        else:
            path, terminal = target, True
        gr = groups.setdefault(path, {"path": path, "nodes": 0, "with_records": 0, "terminal": terminal, "_names": []})
        gr["terminal"] = gr["terminal"] and terminal
        gr["nodes"] += 1
        if _records_of(n, counts):
            gr["with_records"] += 1
        gr["_names"].append(n)
    out = []
    for gr in sorted(groups.values(), key=lambda r: r["path"]):
        rows = sorted(gr.pop("_names"), key=lambda n: n["qualified_name"])
        row = {"path": gr["path"], "nodes": gr["nodes"], "with_records": gr["with_records"],
               "node_types": _type_counts(rows)}
        if gr["terminal"]:
            names = _by_name(rows, counts)
            row["names"] = names[:limit]
            row["names_total"] = len(names)
            row["folded"] = max(0, len(names) - limit)
            if row["folded"]:
                row["unfold"] = f"provledger graph {gr['path']} --depth {depth} --limit {len(names)}"
                row["narrow"] = f"provledger graph {gr['path']} --depth {depth} --type <{'|'.join(t['node_type'] for t in row['node_types'])}>"
        else:
            row["unfold"] = f"provledger graph {gr['path']} --depth {DEFAULT_DEPTH}"
        out.append(row)
    return {"view": "path", "target": target, "depth": depth, "limit": limit, "groups": out,
            "resolved_as": "area" if as_area else "path prefix", "node_type": node_type,
            "nodes": len(members), "with_records": sum(1 for n in members if _records_of(n, counts))}


def _node_view(nodes: list[dict], edges: list[dict], counts: dict, seeds: list[dict],
               target: str, depth: int, limit: int, node_type: str | None = None) -> dict:
    """A node's neighbourhood out to `depth` hops, one line per neighbour,
    edge type and direction on each — and the name spelled out, because a name
    is what another read takes as its argument. Counts alone end the walk.

    One NAME is one line, however many graph node rows carry it. Caught on the
    live graph: `review_and_complete` printed `consumes ← _close:param:conn`
    twice, because two node rows share that qualified name — two identical lines
    that read as a bug and spend the budget twice. The row count moves onto the
    single line instead. The walk itself still visits every node id, so nothing
    downstream of a duplicated name is lost."""
    shown = {n["id"]: n for n in nodes}
    adj: dict[int, list[tuple[int, str, str]]] = {}
    for e in edges:
        if e["src"] in shown and e["dst"] in shown:
            adj.setdefault(e["src"], []).append((e["dst"], e["edge_type"], "out"))
            adj.setdefault(e["dst"], []).append((e["src"], e["edge_type"], "in"))
    seen = {n["id"] for n in seeds}
    frontier = [n["id"] for n in seeds]
    hops = []
    for hop in range(1, max(1, depth) + 1):
        found: dict[int, tuple[str, str]] = {}
        for nid in frontier:
            for other, etype, direction in adj.get(nid, ()):
                if other in seen or other in found:
                    continue
                found[other] = (etype, direction)
        if not found:
            break
        seen |= set(found)
        frontier = list(found)
        available = _type_counts([shown[i] for i in found])
        keep = {i: v for i, v in found.items() if not node_type or shown[i]["node_type"] == node_type}
        by_name: dict[tuple, dict] = {}
        for i, (et, d) in keep.items():
            n = shown[i]
            key = (n["qualified_name"], et, d)
            row = by_name.get(key)
            if row is None:
                by_name[key] = {"qualified_name": n["qualified_name"], "node_type": n["node_type"],
                                "node_key": n["node_key"], "edge_type": et, "direction": d,
                                "records": _records_of(n, counts), "nodes": 1,
                                "unfold": f"provledger graph {n['qualified_name']} --depth {DEFAULT_DEPTH}"}
            else:
                row["nodes"] += 1
                row["records"] = max(row["records"], _records_of(n, counts))
                row["node_key"] = row["node_key"] or n["node_key"]
        rows = sorted(by_name.values(), key=lambda r: (r["qualified_name"], r["edge_type"], r["direction"]))
        # `total` counts deduped NAMES after --type; `nodes_at_hop` and the type
        # breakdown count graph NODES before it. Two different things, so the
        # header says which is which rather than putting them side by side and
        # letting the reader assume they add up.
        entry = {"hop": hop, "neighbours": rows[:limit], "total": len(rows), "folded": max(0, len(rows) - limit),
                 "nodes_at_hop": len(found), "node_types_available": sorted(available, key=lambda t: t["node_type"])}
        if entry["folded"]:
            entry["unfold"] = f"provledger graph {target} --depth {depth} --limit {len(rows)}"
            entry["narrow"] = f"provledger graph {target} --depth {depth} --type <{'|'.join(t['node_type'] for t in entry['node_types_available'])}>"
        hops.append(entry)
    head = seeds[0]
    return {"view": "node", "target": target, "depth": depth, "limit": limit, "hops": hops, "node_type": node_type,
            "node": {"qualified_name": head["qualified_name"], "node_type": head["node_type"],
                     "node_key": head["node_key"], "file_path": head["file_path"],
                     "records": _records_of(head, counts), "matched": len(seeds)},
            "history": f"provledger why {head['qualified_name']}"}


def unfold(psg_db_path: str | None, target: str, *, depth: int = DEFAULT_DEPTH, limit: int = DEFAULT_LIMIT,
           ledger_conn=None, project: str | None = None, include_imports: bool = False,
           node_type: str | None = None) -> dict:
    """Unfold one part of the graph — a node, or an area / path prefix.

    The two namespaces overlap: a `file` node's qualified name IS its path, so
    `app/core/load.py` names both a place in the repo and a node in the graph.
    A `/` in the target decides it — a place in the repo when it has one, a name
    in the code when it does not — which keeps the fold chain the root view
    promises intact (area → directory → file → node names), since a file row's
    own command has to open the file's contents and not the file node's edges.

    What resolves neither way says so and points back at the root view instead
    of guessing: guessing which node was meant is a small act of selection, and
    selection is the model's, never ours."""
    g = _open(psg_db_path)
    if g is None:
        return {"view": "unknown", "target": target, "graph": None, "reason": "no state graph for this project"}
    try:
        nodes = _nodes(g, include_imports)
        edges = _edges(g)
    finally:
        g.close()
    counts = _record_counts(ledger_conn, project)
    shown = [n for n in nodes if not n["excluded"]]
    t = (target or "").strip()
    seeds = [n for n in shown if n["node_key"] == t] if t.startswith("nk_") else [n for n in shown if n["qualified_name"] == t]
    base = _path_segments(t)
    as_path = bool(base) and any(_under(n, base) for n in shown)
    # An exact area key first, so the row's count and the row's command agree;
    # then a `/` means a place in the repo and no `/` means a name in the code.
    if t in {psg_bridge.subsystem_of(n["file_path"]) for n in shown}:
        return _path_view(shown, counts, t, depth, limit, as_area=True, node_type=node_type)
    if "/" in t and as_path:
        return _path_view(shown, counts, "/".join(base), depth, limit, node_type=node_type)
    if seeds:
        return _node_view(shown, edges, counts, seeds, t, depth, limit, node_type)
    if as_path:
        return _path_view(shown, counts, "/".join(base), depth, limit, node_type=node_type)
    return {"view": "unknown", "target": target, "graph": psg_db_path,
            "reason": "no node with that name and no path with that prefix in the graph"}


# ── rendering ────────────────────────────────────────────────────────────────

_ARROW = {"out": "→", "in": "←"}
_FOOTER = ("── how to go on\n"
           "   provledger graph <area|path|node> [--depth N] [--limit M] [--type T]   unfold any branch, to any depth\n"
           "   provledger why <node>                                       that node's history, bounded\n"
           "   provledger record #<id>                                     one record, whole and uncut")


def render(doc: dict) -> str:
    """The human form. Every folded thing prints its size and its command; the
    reader decides what to open next, and nothing here decides for them."""
    if doc.get("view") == "areas":
        return _render_areas(doc)
    if doc.get("view") == "path":
        return _render_path(doc)
    if doc.get("view") == "node":
        return _render_node(doc)
    return (f"{doc.get('target') or '(root)'}: {doc.get('reason', 'nothing to show')}\n"
            "   provledger graph   the whole project, folded to areas\n" + _FOOTER)


def _render_areas(doc: dict) -> str:
    if doc.get("graph") is None:
        return (f"{doc.get('project') or '(project)'}: no state graph registered for this project — "
                "nothing to fold, and nothing is being hidden")
    t = doc["totals"]
    lines = [f"{doc.get('project') or '(project)'} · graph folded to areas · {t['nodes_shown']} nodes · "
             f"{t['edges_shown']} edges · {t['with_records']} of those nodes carry ledger records",
             f"   {doc['folding']}",
             "",
             f"── relations · {len(doc['relations'])} kinds"]
    lines += [f"   {r['edge_type']} · {r['edges']}" for r in doc["relations"]]
    lines += ["", f"── areas · {len(doc['areas'])}"]
    for a in doc["areas"]:
        lines.append(f"   {a['area']} · nodes {a['nodes']} · with records {a['with_records']} · {a['unfold']}")
    ex = doc["excluded"]
    lines.append("")
    if ex["nodes"]:
        named = " · ".join(f"{n['name']} {n['nodes']}" for n in ex["names"])
        more = f" (+{ex.get('distinct_names', 0) - len(ex['names'])} more names)" if ex.get("distinct_names", 0) > len(ex["names"]) else ""
        lines += [f"── excluded · {ex['nodes']} nodes and {ex['edges']} edges · {ex['rule']}",
                  f"   {named}{more}",
                  f"   `{ex['include_with']}` keeps them"]
    else:
        lines.append("── excluded · nothing: every node in the graph is shown")
    lines += ["", _FOOTER]
    return "\n".join(lines)


def _render_path(doc: dict) -> str:
    lines = [f"{doc['target']} · resolved as an {doc['resolved_as']}" if doc.get("resolved_as") == "area"
             else f"{doc['target']} · resolved as a {doc.get('resolved_as', 'path prefix')}",
             f"   {len(doc['groups'])} groups · {doc['nodes']} nodes · "
             f"{doc['with_records']} carry ledger records · depth {doc['depth']}"]
    if doc.get("node_type"):
        lines.append(f"   narrowed to node_type {doc['node_type']} by request")
    for g in doc["groups"]:
        lines.append(f"── {g['path']} · nodes {g['nodes']} · with records {g['with_records']} · "
                     + " · ".join(f"{t['node_type']} {t['nodes']}" for t in g.get("node_types") or ()))
        for n in g.get("names") or ():
            lines.append(f"   {n['qualified_name']} · {n['node_type']} · records {n['records']}"
                         + (f" · {n['nodes']} graph nodes" if n.get("nodes", 1) > 1 else "")
                         + (f" · {n['node_key']}" if n["node_key"] else ""))
        if g.get("folded"):
            lines.append(f"   … {len(g.get('names') or ())} of {g.get('names_total', g['nodes'])} names shown, {g['folded']} folded")
            lines.append(f"      all of them: {g['unfold']}")
            lines.append(f"      one kind:    {g['narrow']}")
        elif g.get("names") is None:
            lines.append(f"   folded (deeper than depth {doc['depth']}) — {g['unfold']}")
    lines += ["", _FOOTER]
    return "\n".join(lines)


def _render_node(doc: dict) -> str:
    n = doc["node"]
    head = (f"{n['qualified_name']} · {n['node_type']} · {n['file_path'] or '(no file)'} · "
            f"records {n['records']} · {doc['history']}")
    lines = [head + (f" · {n['matched']} graph nodes carry this name" if n["matched"] > 1 else "")]
    if doc.get("node_type"):
        lines.append(f"   narrowed to node_type {doc['node_type']} by request")
    for hop in doc["hops"]:
        lines.append(f"── hop {hop['hop']} · {hop['total']} names · of {hop.get('nodes_at_hop')} graph nodes at this hop")
        lines.append(f"   kinds among those {hop.get('nodes_at_hop')} nodes, before --type: "
                     + " · ".join(f"{t['node_type']} {t['nodes']}" for t in hop.get("node_types_available") or ()))
        for nb in hop["neighbours"]:
            lines.append(f"   {nb['edge_type']} {_ARROW.get(nb['direction'], '·')} {nb['qualified_name']} · "
                         f"{nb['node_type']} · records {nb['records']}"
                         + (f" · {nb['nodes']} graph nodes" if nb.get("nodes", 1) > 1 else "")
                         + f" · {nb['unfold']}")
        if hop.get("folded"):
            lines.append(f"   … {len(hop['neighbours'])} of {hop['total']} shown, {hop['folded']} folded")
            lines.append(f"      all of them: {hop['unfold']}")
            lines.append(f"      one kind:    {hop['narrow']}")
    if not doc["hops"]:
        lines.append("── no neighbours in the graph at this depth")
    lines += ["", _FOOTER]
    return "\n".join(lines)
