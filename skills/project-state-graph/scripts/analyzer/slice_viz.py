"""slice_viz.py — provLedger Phase B DataFrame-aware visualization slices.

A peer to ``architecture_md.py`` / ``graph_viz.py``: reads a built
``*-state-graph.db`` and renders FOUR per-perspective slices into ONE
self-contained HTML file (vis-network from the same CDN graph_viz uses, all data
embedded inline so it opens over ``file://`` with no server):

  1. **Dataflow + datatype** — data_var / return nodes at variable/column
     granularity along ``produces`` / ``consumes`` / ``feeds`` /
     ``downstream_data_feed`` edges, each labeled with its dtype. ``unknown``
     dtypes render **gray with a ``?``** so coverage holes are visible at a glance.
  2. **Function call chain** — a focus function's callers (above) + callees
     (below) within N hops; the neighborhood, not the whole graph.
  3. **Pipeline view** — ``pipeline_step`` edges in execution order.
  4. **API surface** — a TABLE: path · method · handler · what the handler calls.

Stdlib only (``sqlite3`` + ``json`` + a string template). Imports NOTHING from
the analyzer package, so this module is byte-portable into the reviewer skill.
"""
from __future__ import annotations

import html as _html
import json
import os
import sqlite3
from typing import Dict, List, Optional


def _safe_json(data) -> str:
    """json.dumps hardened for <script> embedding (PSG-S1): escape </script>
    breakout and U+2028/U+2029."""
    return (json.dumps(data)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace(" ", "\\u2028").replace(" ", "\\u2029"))

# ── palette ────────────────────────────────────────────────────────────
BRAND_BLUE = "#2a78d6"
SPARK = "#fab219"
GREEN = "#1baf7a"
ORANGE = "#eb6834"
GRAY = "#898781"           # the "unknown dtype" sentinel color (muted ink)

UNKNOWN_DTYPES = {"", "unknown", "any", "object?"}

FLOW_EDGES = ("produces", "consumes", "feeds", "downstream_data_feed")
DATA_NODE_TYPES = ("data_var", "column", "dataframe", "dataset")


# ── helpers ──────────────────────────────────────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _meta(raw) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def _is_unknown(dtype: Optional[str]) -> bool:
    return dtype is None or str(dtype).strip().lower() in UNKNOWN_DTYPES


# ── slice 1 · dataflow + datatype ────────────────────────────────────────────────

def build_dataflow(db_path: str) -> Dict:
    """Variable/column-granularity dataflow slice with dtype coverage.

    Returns ``{nodes, edges, dtype_coverage:{typed, unknown, pct}}``. Unknown
    dtypes are gray (#898781) with a trailing ``?`` in the label; typed nodes use
    the brand palette. Flow edges carry a dtype ``label`` when known.
    """
    conn = _connect(db_path)
    try:
        dt_ph = ",".join("?" for _ in DATA_NODE_TYPES)
        node_rows = conn.execute(
            f"""SELECT n.id, n.name, n.qualified_name, n.file_path,
                       n.metadata_json, t.name AS ntype
                FROM node n JOIN node_type t ON n.node_type_id = t.id
                WHERE t.name IN ({dt_ph})""",
            DATA_NODE_TYPES,
        ).fetchall()

        e_ph = ",".join("?" for _ in FLOW_EDGES)
        edge_rows = conn.execute(
            f"""SELECT e.src_node_id, e.dst_node_id, t.name AS etype,
                       e.metadata_json
                FROM edge e JOIN edge_type t ON e.edge_type_id = t.id
                WHERE t.name IN ({e_ph})""",
            FLOW_EDGES,
        ).fetchall()

        # also pull the producing/consuming function nodes referenced by flow
        # edges so the slice shows what produces/consumes each value.
        fn_rows = conn.execute(
            """SELECT n.id, n.name, n.qualified_name, n.file_path, t.name AS ntype
               FROM node n JOIN node_type t ON n.node_type_id = t.id
               WHERE t.name IN ('function', 'method', 'route')"""
        ).fetchall()
    finally:
        conn.close()

    nodes: List[Dict] = []
    typed = unknown = 0
    data_ids = set()
    for r in node_rows:
        meta = _meta(r["metadata_json"])
        dtype = meta.get("dtype")
        is_unknown = _is_unknown(dtype)
        if is_unknown:
            unknown += 1
        else:
            typed += 1
        shown_dtype = "unknown" if is_unknown else str(dtype)
        label = r["name"] + (" ?" if is_unknown else "")
        title = (f"{r['ntype']}: {r['qualified_name'] or r['name']}\n"
                 f"dtype: {shown_dtype}")
        if meta.get("dtype_provenance"):
            title += f" ({meta['dtype_provenance']})"
        if r["file_path"]:
            title += f"\n{r['file_path']}"
        nodes.append({
            "id": r["id"], "label": label, "node_type": r["ntype"],
            "dtype": shown_dtype, "unknown": is_unknown,
            "color": GRAY if is_unknown else GREEN,
            "shape": "diamond", "title": title,
        })
        data_ids.add(r["id"])

    # include referenced function nodes (lightweight, blue dots)
    fn_by_id = {r["id"]: r for r in fn_rows}
    referenced_fns = set()
    edges: List[Dict] = []
    for r in edge_rows:
        s, d = r["src_node_id"], r["dst_node_id"]
        meta = _meta(r["metadata_json"])
        dtype = meta.get("dtype") or meta.get("type")
        edge = {"from": s, "to": d, "edge_type": r["etype"], "arrows": "to"}
        if dtype and not _is_unknown(dtype):
            edge["label"] = str(dtype)
        edges.append(edge)
        for nid in (s, d):
            if nid in fn_by_id:
                referenced_fns.add(nid)

    for nid in referenced_fns:
        r = fn_by_id[nid]
        nodes.append({
            "id": r["id"], "label": r["name"], "node_type": r["ntype"],
            "dtype": "", "unknown": False, "color": BRAND_BLUE,
            "shape": "dot",
            "title": f"{r['ntype']}: {r['qualified_name'] or r['name']}",
        })

    keep = data_ids | referenced_fns
    edges = [e for e in edges if e["from"] in keep and e["to"] in keep]

    total = typed + unknown
    pct = round(100.0 * typed / total, 1) if total else 0
    return {"nodes": nodes, "edges": edges,
            "dtype_coverage": {"typed": typed, "unknown": unknown, "pct": pct}}


# ── slice 2 · function call chain ────────────────────────────────────────────────

CALLABLE_TYPES = ("function", "method", "route")


def build_call_chain(db_path: str, focus: str, hops: int = 1) -> Dict:
    """A focus function's caller/callee neighborhood within ``hops`` levels.

    ``focus`` is matched against ``qualified_name`` first, then ``name``. Callers
    (incoming ``calls`` edges) are walked UP and callees (outgoing) DOWN, each up
    to ``hops`` levels. Each node is tagged ``direction`` (focus/caller/callee).
    Unknown focus -> ``{nodes:[], edges:[], focus_id:None}``.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """SELECT n.id, n.name, n.qualified_name, n.file_path
               FROM node n JOIN node_type t ON n.node_type_id = t.id
               WHERE t.name IN ('function','method','route')
                 AND (n.qualified_name = ? OR n.name = ?)
               LIMIT 1""",
            (focus, focus),
        ).fetchone()
        if row is None:
            return {"nodes": [], "edges": [], "focus_id": None}
        focus_id = row["id"]

        call_edges = conn.execute(
            """SELECT e.src_node_id AS s, e.dst_node_id AS d
               FROM edge e JOIN edge_type t ON e.edge_type_id = t.id
               WHERE t.name = 'calls'"""
        ).fetchall()
        meta_rows = conn.execute(
            """SELECT n.id, n.name, n.qualified_name, n.file_path, t.name AS ntype
               FROM node n JOIN node_type t ON n.node_type_id = t.id"""
        ).fetchall()
    finally:
        conn.close()

    callers_of: Dict[int, List[int]] = {}
    callees_of: Dict[int, List[int]] = {}
    for e in call_edges:
        callees_of.setdefault(e["s"], []).append(e["d"])
        callers_of.setdefault(e["d"], []).append(e["s"])

    direction: Dict[int, str] = {focus_id: "focus"}

    def walk(start: int, adj: Dict[int, List[int]], tag: str) -> None:
        frontier = [start]
        for _ in range(hops):
            nxt = []
            for cur in frontier:
                for nb in adj.get(cur, []):
                    if nb not in direction:
                        direction[nb] = tag
                        nxt.append(nb)
            frontier = nxt

    walk(focus_id, callers_of, "caller")
    walk(focus_id, callees_of, "callee")

    info = {r["id"]: r for r in meta_rows}
    keep = set(direction)
    color = {"focus": SPARK, "caller": BRAND_BLUE, "callee": GREEN}
    nodes = []
    for nid in keep:
        r = info.get(nid)
        if r is None:
            continue
        d = direction[nid]
        nodes.append({
            "id": nid, "label": r["name"], "direction": d,
            "node_type": r["ntype"], "color": color[d],
            "shape": "star" if d == "focus" else "dot",
            "title": f"{r['ntype']}: {r['qualified_name'] or r['name']}"
                     f"\n[{d}]" + (f"\n{r['file_path']}" if r["file_path"] else ""),
        })

    edges = [{"from": e["s"], "to": e["d"], "edge_type": "calls", "arrows": "to"}
             for e in call_edges if e["s"] in keep and e["d"] in keep]

    return {"nodes": nodes, "edges": edges, "focus_id": focus_id}


# ── slice 3 · pipeline view ──────────────────────────────────────────────────────

def build_pipeline(db_path: str) -> Dict:
    """Pipelines reconstructed from ``pipeline_step`` edges.

    Edges are grouped by ``metadata_json.pipeline`` and ordered by
    ``metadata_json.index``. Each pipeline yields an ordered ``steps`` list of
    ``{index, fn}`` (the step's source function) followed by the final
    destination, plus sequential ``edges``. Self-steps (src==dst) are tolerated.
    No ``pipeline_step`` edges -> ``{pipelines: []}``.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT e.src_node_id AS s, e.dst_node_id AS d, e.metadata_json AS m,
                      sn.name AS sname, dn.name AS dname
               FROM edge e
               JOIN edge_type t ON e.edge_type_id = t.id
               JOIN node sn ON sn.id = e.src_node_id
               JOIN node dn ON dn.id = e.dst_node_id
               WHERE t.name = 'pipeline_step'"""
        ).fetchall()
    finally:
        conn.close()

    grouped: Dict[str, List[dict]] = {}
    for r in rows:
        meta = _meta(r["m"])
        name = meta.get("pipeline", "(unnamed)")
        grouped.setdefault(name, []).append({
            "index": meta.get("index", 0),
            "s": r["s"], "d": r["d"],
            "sname": r["sname"], "dname": r["dname"],
        })

    pipelines = []
    for name in sorted(grouped):
        items = sorted(grouped[name], key=lambda x: x["index"])
        steps = []
        edges = []
        for it in items:
            steps.append({"index": it["index"], "fn": it["sname"],
                          "node_id": it["s"]})
            edges.append({"from": it["s"], "to": it["d"],
                          "edge_type": "pipeline_step", "arrows": "to"})
        # append the final destination as the terminal step if distinct
        if items:
            last = items[-1]
            if not steps or steps[-1]["node_id"] != last["d"]:
                steps.append({"index": last["index"] + 1, "fn": last["dname"],
                              "node_id": last["d"]})
        pipelines.append({"name": name, "steps": steps, "edges": edges})

    return {"pipelines": pipelines}


# ── slice 4 · API surface (table) ────────────────────────────────────────────────

def build_api_surface(db_path: str) -> List[Dict]:
    """The API surface as TABLE rows (not a graph).

    One row per ``handles`` edge (route -> handler function):
    ``{path, method, handler, handler_file, calls}`` where ``calls`` are the
    handler's outgoing ``calls`` targets (names, sorted + deduped). No routes
    -> ``[]``.
    """
    conn = _connect(db_path)
    try:
        handles = conn.execute(
            """SELECT r.id AS route_id, r.name AS path, r.metadata_json AS rmeta,
                      h.id AS handler_id, h.name AS handler, h.file_path AS hfile
               FROM edge e
               JOIN edge_type t ON e.edge_type_id = t.id
               JOIN node r ON r.id = e.src_node_id
               JOIN node h ON h.id = e.dst_node_id
               WHERE t.name = 'handles'"""
        ).fetchall()
        call_rows = conn.execute(
            """SELECT e.src_node_id AS s, n.name AS callee
               FROM edge e
               JOIN edge_type t ON e.edge_type_id = t.id
               JOIN node n ON n.id = e.dst_node_id
               WHERE t.name = 'calls'"""
        ).fetchall()
    finally:
        conn.close()

    calls_by_fn: Dict[int, set] = {}
    for r in call_rows:
        calls_by_fn.setdefault(r["s"], set()).add(r["callee"])

    rows = []
    for h in handles:
        meta = _meta(h["rmeta"])
        rows.append({
            "path": h["path"],
            "method": meta.get("method", "?"),
            "handler": h["handler"],
            "handler_file": h["hfile"],
            "calls": sorted(calls_by_fn.get(h["handler_id"], set())),
        })
    rows.sort(key=lambda r: r["path"])
    return rows


# ── self-contained HTML writer ───────────────────────────────────────────────────

_SLICE_TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>__TITLE__ — slices</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
 *{box-sizing:border-box} body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#0b0b0b}
 header{background:#fcfcfb;color:#0b0b0b;border-bottom:1px solid #e1e0d9;padding:10px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 header h1{font-size:16px;margin:0;font-weight:600}
 .badge{background:rgba(250,178,25,.18);color:#7a5200;font-size:12px;font-weight:600;padding:3px 8px;border-radius:10px}
 .tabs{display:flex;gap:4px;background:#f9f9f7;border-bottom:1px solid #e1e0d9;padding:6px 10px}
 .tab{font-size:13px;padding:6px 12px;border:1px solid #d6d4cc;background:#fff;border-radius:6px;cursor:pointer}
 .tab.active{background:#2a78d6;color:#fff;border-color:#2a78d6}
 .panel{display:none;height:calc(100vh - 96px)} .panel.active{display:block}
 .net{width:100%;height:100%}
 .toolbar{padding:6px 10px;font-size:12px;color:#52514e;display:flex;gap:10px;align-items:center}
 .toolbar input{padding:4px 8px;border:1px solid #d6d4cc;border-radius:6px}
 table{border-collapse:collapse;width:100%;font-size:13px} th,td{border:1px solid #e1e0d9;padding:6px 10px;text-align:left;vertical-align:top}
 th{background:#f9f9f7} code{background:#f0efec;padding:1px 5px;border-radius:4px;font-size:12px}
 .legend{font-size:12px;color:#52514e;padding:4px 10px}
 .sw{display:inline-block;width:11px;height:11px;border-radius:3px;vertical-align:middle;margin-right:4px}
</style></head><body>
<header><h1>__TITLE__ · provLedger slices</h1>
 <span class="badge" id="cov">dtype coverage: __COVPCT__% (__COVTYPED__ typed / __COVUNK__ unknown)</span></header>
<div class="tabs">
 <button class="tab active" data-p="dataflow">① Dataflow + datatype</button>
 <button class="tab" data-p="callchain">② Call chain</button>
 <button class="tab" data-p="pipeline">③ Pipeline</button>
 <button class="tab" data-p="api">④ API surface</button>
</div>
<div id="dataflow" class="panel active">
 <div class="legend"><span class="sw" style="background:#1baf7a"></span>typed
  <span class="sw" style="background:#898781"></span>unknown dtype (?)
  <span class="sw" style="background:#2a78d6"></span>function</div>
 <div class="net" id="net_dataflow"></div></div>
<div id="callchain" class="panel">
 <div class="toolbar">Focus:&nbsp;<input id="focusInput" placeholder="qualified_name"><button id="focusBtn">Show</button>
  <span class="legend"><span class="sw" style="background:#fab219"></span>focus
   <span class="sw" style="background:#2a78d6"></span>caller
   <span class="sw" style="background:#1baf7a"></span>callee</span></div>
 <div class="net" id="net_callchain" style="height:calc(100% - 40px)"></div></div>
<div id="pipeline" class="panel"><div class="net" id="net_pipeline"></div></div>
<div id="api" class="panel" style="overflow:auto;padding:12px">
 <table><thead><tr><th>Path</th><th>Method</th><th>Handler</th><th>File</th><th>Calls</th></tr></thead>
 <tbody id="apiBody"></tbody></table></div>
<script>
const DATA=__DATA__;
function styleEdges(es){es.forEach(e=>{e.arrows=e.arrows||'to';e.color=e.color||{color:'#c3c2b7',opacity:0.7};
 e.smooth=e.smooth||{type:'continuous'};if(e.label)e.font={size:10,color:'#52514e'};});return es;}
function mkNet(elId,nodes,edges,opts){return new vis.Network(document.getElementById(elId),
 {nodes:new vis.DataSet(nodes),edges:new vis.DataSet(styleEdges(edges))},
 Object.assign({nodes:{font:{size:12}},edges:{},interaction:{hover:true,tooltipDelay:120},
  physics:{stabilization:true,barnesHut:{gravitationalConstant:-7000,springLength:130,avoidOverlap:0.3}}},opts||{}));}
// slice 1
mkNet('net_dataflow',DATA.dataflow.nodes,DATA.dataflow.edges);
// slice 3 pipeline (hierarchical)
const pipeNodes=[],pipeEdges=[];
DATA.pipeline.pipelines.forEach((p,pi)=>{p.steps.forEach(s=>{pipeNodes.push({id:'p'+pi+'_'+s.node_id+'_'+s.index,
 label:s.index+'. '+s.fn,color:'#2a78d6',shape:'box',font:{color:'#fff',size:12}});});
 for(let i=0;i<p.steps.length-1;i++){pipeEdges.push({from:'p'+pi+'_'+p.steps[i].node_id+'_'+p.steps[i].index,
  to:'p'+pi+'_'+p.steps[i+1].node_id+'_'+p.steps[i+1].index});}});
mkNet('net_pipeline',pipeNodes,pipeEdges,{layout:{hierarchical:{direction:'LR',sortMethod:'directed',levelSeparation:160}},physics:false});
// slice 4 api table
const tb=document.getElementById('apiBody');
DATA.api.forEach(r=>{const tr=document.createElement('tr');
 tr.innerHTML='<td><code>'+r.path+'</code></td><td>'+r.method+'</td><td>'+r.handler+'</td><td>'+(r.handler_file||'')+'</td><td>'+
  (r.calls.length?r.calls.map(c=>'<code>'+c+'</code>').join(' '):'<span style="color:#898781">—</span>')+'</td>';
 tb.appendChild(tr);});
// slice 2 call chain (interactive focus)
let ccNet=null;
function renderCC(focus){const all=DATA.callchain_all;
 // build adjacency from the precomputed full calls list, then neighborhood in JS
 const byName={},nodes=all.nodes,edges=all.edges;
 nodes.forEach(n=>{byName[n.qualified_name]=n.id;byName[n.label]=byName[n.label]||n.id;});
 let fid=byName[focus];
 const elId='net_callchain';
 if(fid==null){document.getElementById(elId).innerHTML='<p style="padding:12px;color:#898781">No function named '+focus+'</p>';return;}
 const callers={},callees={};edges.forEach(e=>{(callees[e.from]=callees[e.from]||[]).push(e.to);(callers[e.to]=callers[e.to]||[]).push(e.from);});
 const dir={};dir[fid]='focus';[['caller',callers],['callee',callees]].forEach(([tag,adj])=>{(adj[fid]||[]).forEach(n=>{if(!(n in dir))dir[n]=tag;});});
 const col={focus:'#fab219',caller:'#2a78d6',callee:'#1baf7a'};
 const vn=nodes.filter(n=>n.id in dir).map(n=>Object.assign({},n,{color:col[dir[n.id]],shape:dir[n.id]=='focus'?'star':'dot'}));
 const ids=new Set(vn.map(n=>n.id));const ve=edges.filter(e=>ids.has(e.from)&&ids.has(e.to)).map(e=>Object.assign({},e));
 document.getElementById(elId).innerHTML='';ccNet=mkNet(elId,vn,ve);}
document.getElementById('focusBtn').onclick=()=>renderCC(document.getElementById('focusInput').value.trim());
if(DATA.callchain_default){document.getElementById('focusInput').value=DATA.callchain_default;renderCC(DATA.callchain_default);}
// tabs
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
 document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
 document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));
 t.classList.add('active');document.getElementById(t.dataset.p).classList.add('active');});
</script></body></html>"""


def _default_focus(db_path: str) -> str:
    """Pick a reasonable default focus for the call-chain slice: the most-called
    function (highest incoming calls)."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """SELECT n.qualified_name AS q, COUNT(*) AS c
               FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
               JOIN node n ON n.id = e.dst_node_id
               WHERE t.name='calls'
               GROUP BY e.dst_node_id ORDER BY c DESC LIMIT 1"""
        ).fetchone()
        return row["q"] if row and row["q"] else ""
    except sqlite3.OperationalError:
        return ""
    finally:
        conn.close()


def _all_calls(db_path: str) -> Dict:
    """Full callable node list + calls edges, for the in-browser call-chain focus."""
    conn = _connect(db_path)
    try:
        nodes = [{"id": r["id"], "label": r["name"],
                  "qualified_name": r["qualified_name"] or r["name"]}
                 for r in conn.execute(
                     """SELECT n.id, n.name, n.qualified_name
                        FROM node n JOIN node_type t ON n.node_type_id=t.id
                        WHERE t.name IN ('function','method','route')""").fetchall()]
        edges = [{"from": r["s"], "to": r["d"]} for r in conn.execute(
            """SELECT e.src_node_id AS s, e.dst_node_id AS d
               FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
               WHERE t.name='calls'""").fetchall()]
    finally:
        conn.close()
    return {"nodes": nodes, "edges": edges}


def write_slices(db_path: str, out_path: str, *, title: str = "state-graph") -> str:
    """Render all four slices into one self-contained HTML file (file://-safe)."""
    dataflow = build_dataflow(db_path)
    pipeline = build_pipeline(db_path)
    api = build_api_surface(db_path)
    cov = dataflow["dtype_coverage"]
    data = {
        "dataflow": {"nodes": dataflow["nodes"], "edges": dataflow["edges"]},
        "pipeline": pipeline,
        "api": api,
        "callchain_all": _all_calls(db_path),
        "callchain_default": _default_focus(db_path),
    }
    html = (_SLICE_TEMPLATE
            .replace("__TITLE__", _html.escape(title))       # PSG-S1
            .replace("__COVPCT__", str(cov["pct"]))
            .replace("__COVTYPED__", str(cov["typed"]))
            .replace("__COVUNK__", str(cov["unknown"]))
            .replace("__DATA__", _safe_json(data)))          # PSG-S1: </script>-safe
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path
