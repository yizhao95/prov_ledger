"""Multi-level, self-contained visualization for a project state graph.

Reads a built ``*-state-graph.db`` and renders an interactive, self-contained
HTML file (vis-network from a CDN, data embedded inline — works over file://).

Three levels:

* ``subsystems`` — high level. Nodes are 2-level directories (``top/second``)
  sized by node count; edges are weighted cross-subsystem dependency links.
  Nodes with no ``file_path`` (external modules / sql stubs) are excluded so the
  map is not dominated by a meaningless bucket.
* ``functions`` — mid level. Nodes are application ``function``/``method``/
  ``route`` symbols (tests excluded), colored by subsystem; edges are ``calls``
  (control flow) and ``downstream_data_feed`` (one function's output feeding
  another — i.e. variable passing). With ``include_data_vars=True`` the actual
  ``data_var`` nodes plus ``produces``/``consumes`` edges are added.
* ``full`` — everything, with a node-type / edge-type filter UI.

Pure ``sqlite3`` + ``json`` + a string template: NO imports from the analyzer
package, so this module is byte-identical-portable into the review skill too.
"""
from __future__ import annotations

import html as _html
import json
import os
import sqlite3
from typing import Dict, List, Tuple

# ── palette ────────────────────────────────────────────────────────────────
BRAND_BLUE = "#2a78d6"
SPARK = "#fab219"
RED = "#e34948"
GREEN = "#1baf7a"

_NODE_COLORS = {
    "file": "#2a78d6", "function": "#1baf7a", "method": "#199e70",
    "class": "#fab219", "module": "#898781", "pipeline": "#e34948",
    "route": "#4a3aa7", "sql_table": "#eb6834", "bq_dataset": "#d95926",
    "css_selector": "#e87ba4", "data_var": "#52514e",
}
# one hue per subsystem (assigned deterministically by sort order)
_SUB_PALETTE = ["#2a78d6", "#1baf7a", "#4a3aa7", "#eb6834", "#e87ba4",
                "#e34948", "#199e70", "#d95926", "#52514e", "#d55181"]

APP_FUNC_TYPES = ("function", "method", "route")
FLOW_EDGES = ("calls", "downstream_data_feed", "produces", "consumes", "feeds")
_TEST_PRED = "(n.file_path LIKE '%/tests/%' OR n.file_path LIKE 'tests/%')"


def _profile_map(conn: sqlite3.Connection) -> Dict[int, List[str]]:
    """symbol node id -> sorted list of profile names (from tagged_profile edges).

    Returns an empty map if the profile/tagged_profile vocabulary is absent
    (older graphs), so the viz degrades gracefully.
    """
    try:
        rows = conn.execute(
            """SELECT e.src_node_id, p.name
               FROM edge e
               JOIN edge_type t ON e.edge_type_id=t.id
               JOIN node p ON e.dst_node_id=p.id
               WHERE t.name='tagged_profile'"""
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: Dict[int, List[str]] = {}
    for sid, pname in rows:
        out.setdefault(int(sid), []).append(pname)
    for sid in out:
        out[sid] = sorted(set(out[sid]))
    return out


def _all_profiles(pmap: Dict[int, List[str]]) -> List[str]:
    return sorted({p for ps in pmap.values() for p in ps})


def subsystem_of(file_path: str) -> str:
    """Top two path segments => subsystem key. No path => '(external)'."""
    if not file_path:
        return "(external)"
    parts = file_path.replace("\\", "/").lstrip("./").split("/")
    if len(parts) >= 3:
        return parts[0] + "/" + parts[1]
    if len(parts) == 2:
        return parts[0]
    return "(root)"


# ── data builders ───────────────────────────────────────────────────────────

def build_subsystems(db_path: str) -> Dict:
    conn = sqlite3.connect(db_path)
    try:
        nodes = conn.execute("SELECT id, file_path FROM node").fetchall()
        edges = conn.execute(
            "SELECT src_node_id, dst_node_id FROM edge"
        ).fetchall()
    finally:
        conn.close()

    node_sub: Dict[int, str] = {}
    for nid, fp in nodes:
        if not fp:
            continue  # drop external/unknown so they don't form a mega-bucket
        node_sub[nid] = subsystem_of(fp)

    counts: Dict[str, int] = {}
    for s in node_sub.values():
        counts[s] = counts.get(s, 0) + 1

    dep: Dict[Tuple[str, str], int] = {}
    for src, dst in edges:
        ss, ds = node_sub.get(src), node_sub.get(dst)
        if ss is None or ds is None or ss == ds:
            continue
        dep[(ss, ds)] = dep.get((ss, ds), 0) + 1

    subs = sorted(counts)
    color = {s: _SUB_PALETTE[i % len(_SUB_PALETTE)] for i, s in enumerate(subs)}
    max_c = max(counts.values()) if counts else 1
    vis_nodes = [{
        "id": s, "label": f"{s}\n({counts[s]})", "value": counts[s],
        "size": 18 + 42 * (counts[s] / max_c),
        "color": SPARK if "/tests" in s or s == "tests" else color[s],
        "font": {"size": 14, "multi": True}, "title": f"{s}: {counts[s]} nodes",
    } for s in subs]

    max_w = max(dep.values()) if dep else 1
    vis_edges = [{
        "from": ss, "to": ds, "value": w, "width": 1 + 7 * (w / max_w),
        "title": f"{ss} \u2192 {ds}: {w} refs", "arrows": "to",
        "color": {"color": "#c3c2b7", "opacity": 0.7},
        "smooth": {"type": "curvedCW", "roundness": 0.15},
    } for (ss, ds), w in dep.items()]

    legend = [{"sub": s, "color": color[s]} for s in subs]
    return {"nodes": vis_nodes, "edges": vis_edges, "legend": legend,
            "node_types": [], "edge_types": [], "profiles": []}


def build_functions(db_path: str, include_data_vars: bool = False) -> Dict:
    want = list(APP_FUNC_TYPES) + (["data_var"] if include_data_vars else [])
    conn = sqlite3.connect(db_path)
    try:
        ph = ",".join("?" for _ in want)
        node_rows = conn.execute(
            f"""SELECT n.id, n.name, n.qualified_name, n.file_path, t.name
                FROM node n JOIN node_type t ON n.node_type_id=t.id
WHERE t.name IN ({ph}) AND NOT {_TEST_PRED}""",
            want,
        ).fetchall()
        eph = ",".join("?" for _ in FLOW_EDGES)
        edge_rows = conn.execute(
            f"""SELECT e.src_node_id, e.dst_node_id, t.name
                FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
                WHERE t.name IN ({eph})""",
            FLOW_EDGES,
        ).fetchall()
        pmap = _profile_map(conn)
        # data_var ids that flow onward (have an outgoing consumes edge)
        consumed_dv = {
            int(s) for (s,) in conn.execute(
                """SELECT DISTINCT e.src_node_id
                   FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
                   WHERE t.name='consumes'"""
            ).fetchall()
        }
        # callable node ids that are actually called somewhere (incoming calls)
        called_fns = {
            int(d) for (d,) in conn.execute(
                """SELECT DISTINCT e.dst_node_id
                   FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
                   WHERE t.name='calls'"""
            ).fetchall()
        }
        # map each return data_var to its producing function id (produces edge)
        producer_of = {
            int(dst): int(src) for src, dst in conn.execute(
                """SELECT e.src_node_id, e.dst_node_id
                   FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
                   WHERE t.name='produces'"""
            ).fetchall()
        }
    finally:
        conn.close()

    subs = sorted({subsystem_of(r[3]) for r in node_rows})
    color = {s: _SUB_PALETTE[i % len(_SUB_PALETTE)] for i, s in enumerate(subs)}

    nodes: List[Dict] = []
    for nid, name, qname, fp, ntype in node_rows:
        sub = subsystem_of(fp)
        is_dv = ntype == "data_var"
        profs = pmap.get(nid, [])
        # A return value is a genuine dead-end only when (a) nothing consumes it
        # AND (b) its producing function is never called anywhere (truly
        # unreachable). A return that IS consumed, or whose function is called
        # (result may flow on in ways static analysis can't follow), is NOT
        # flagged — this avoids the false-positive 'isolated' problem.
        is_return = is_dv and name.endswith(":return")
        producer = producer_of.get(nid)
        producer_called = producer in called_fns if producer is not None else False
        unconsumed = bool(
            is_return and nid not in consumed_dv and not producer_called
        )
        node = {
            "id": nid, "label": name, "node_type": ntype,
            "profiles": profs,
            "title": f"{ntype}: {qname or name}\n{fp}\n[{sub}]"
                     + (f"\nprofiles: {', '.join(profs)}" if profs else "")
                     + ("\n(unconsumed: no downstream consumer found)"
                        if unconsumed else ""),
            "color": SPARK if is_dv else color[sub],
            "shape": "box" if ntype == "route" else ("diamond" if is_dv else "dot"),
            "size": 8 if is_dv else 14, "font": {"size": 11},
        }
        if is_dv:
            node["unconsumed"] = unconsumed
            if unconsumed:
                # dim + dashed border so terminal data reads as a dead-end
                node["color"] = {"background": "#fdeecd", "border": "#c98500"}
                node["shapeProperties"] = {"borderDashes": [4, 4]}
        nodes.append(node)

    keep = {n["id"] for n in nodes}
    edges = [{"from": s, "to": d, "edge_type": t}
             for s, d, t in edge_rows if s in keep and d in keep]

    return {"nodes": nodes, "edges": edges,
            "node_types": sorted({n["node_type"] for n in nodes}),
            "edge_types": sorted({e["edge_type"] for e in edges}),
            "profiles": _all_profiles(pmap),
            "legend": [{"sub": s, "color": color[s]} for s in subs]}


def build_full(db_path: str) -> Dict:
    conn = sqlite3.connect(db_path)
    try:
        node_rows = conn.execute(
            """SELECT n.id, n.name, n.qualified_name, n.file_path,
                      n.line_start, t.name
               FROM node n JOIN node_type t ON n.node_type_id=t.id"""
        ).fetchall()
        edge_rows = conn.execute(
            """SELECT e.src_node_id, e.dst_node_id, t.name, e.metadata_json
               FROM edge e JOIN edge_type t ON e.edge_type_id=t.id"""
        ).fetchall()
        pmap = _profile_map(conn)
    finally:
        conn.close()

    nodes = []
    for nid, name, qname, fpath, line, ntype in node_rows:
        title = [f"{ntype}: {qname or name}"]
        if fpath:
            title.append(f"{fpath}:{line}" if line else fpath)
        profs = pmap.get(nid, [])
        if profs:
            title.append(f"profiles: {', '.join(profs)}")
        nodes.append({
            "id": nid, "label": name, "node_type": ntype,
            "qualified_name": qname, "file_path": fpath,
            "profiles": profs,
            "title": "\n".join(title),
            "color": _NODE_COLORS.get(ntype, "#898781"),
        })
    edges = [{
        "from": s, "to": d, "edge_type": t,
        "meta": json.loads(m) if m else None,
    } for s, d, t, m in edge_rows]

    return {"nodes": nodes, "edges": edges,
            "node_types": sorted({n["node_type"] for n in nodes}),
            "edge_types": sorted({e["edge_type"] for e in edges}),
            "profiles": _all_profiles(pmap), "legend": []}


# ── HTML rendering ───────────────────────────────────────────────────────────

_TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>__TITLE__ — __LEVEL__</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
 *{box-sizing:border-box} body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#0b0b0b}
 header{background:#fcfcfb;color:#0b0b0b;border-bottom:1px solid #e1e0d9;padding:10px 16px;display:flex;gap:16px;align-items:center}
 header h1{font-size:16px;margin:0;font-weight:600} header .stats{font-size:12px;opacity:.9}
 #wrap{display:flex;height:calc(100vh - 44px)}
 #panel{width:240px;overflow:auto;border-right:1px solid #e1e0d9;padding:12px}
 #net{flex:1}
 .group{margin-bottom:16px} .group h2{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#52514e;margin:0 0 8px}
 label{display:flex;align-items:center;gap:8px;font-size:13px;padding:3px 0;cursor:pointer}
 .sw{width:12px;height:12px;border-radius:3px;display:inline-block}
 .count{color:#898781;font-size:11px;margin-left:auto}
 #search{width:100%;padding:6px 8px;margin-bottom:10px;border:1px solid #d6d4cc;border-radius:6px}
 button{font-size:12px;padding:4px 8px;border:1px solid #d6d4cc;background:#f9f9f7;border-radius:6px;cursor:pointer}
 .btn-row{display:flex;gap:6px;margin-bottom:8px}
</style></head><body>
<header><h1>__TITLE__ · __LEVEL__ map</h1><span class="stats" id="stats"></span></header>
<div id="wrap"><div id="panel">
  <input id="search" placeholder="Search node…">
  <div class="btn-row"><button id="fit">Fit</button><button id="physics">Physics</button><button id="clearFocus">Clear focus</button></div>
  <div class="group" id="subflowGroup"><h2>Sub-flow lens</h2>
    <select id="subflow"><option value="">(full graph)</option></select>
    <div id="focusNote" style="font-size:11px;color:#52514e;margin-top:6px">Click a node to focus its end-to-end subgraph.</div>
  </div>
  <div class="group" id="legendGroup"><h2>Subsystems</h2><div id="legend"></div></div>
  <div class="group" id="nodeGroup"><h2>Node types</h2><div id="nodeF"></div></div>
  <div class="group" id="edgeGroup"><h2>Edge types</h2><div id="edgeF"></div></div>
</div><div id="net"></div></div>
<script>
const GRAPH_DATA=__GRAPH_DATA__, G=GRAPH_DATA, LEVEL="__LEVEL__";
const allNodes=G.nodes, allEdges=G.edges.map((e,i)=>Object.assign({id:i},e));
const EDGE_STYLE={calls:{color:'#c3c2b7',dashes:false},
 downstream_data_feed:{color:'#2a78d6',dashes:false},
 feeds:{color:'#2a78d6',dashes:false},
 produces:{color:'#1baf7a',dashes:true}, consumes:{color:'#eb6834',dashes:true}};
allEdges.forEach(e=>{if(e.edge_type){const s=EDGE_STYLE[e.edge_type]||{color:'#c3c2b7'};
 e.color={color:s.color,opacity:0.55}; e.dashes=s.dashes; e.arrows=e.arrows||'to';
 e.smooth=e.smooth||{type:'continuous'};}});
const hasTypes=(G.node_types&&G.node_types.length)>0;
const onN=new Set(G.node_types||[]), onE=new Set(G.edge_types||[]);
const nDS=new vis.DataSet([]), eDS=new vis.DataSet([]);
const net=new vis.Network(document.getElementById('net'),{nodes:nDS,edges:eDS},{
 nodes:{shape:'dot',size:12,font:{size:12},scaling:{min:14,max:60}},
 edges:{arrows:'to',scaling:{min:1,max:8}},
 interaction:{hover:true,tooltipDelay:120},
 physics:{stabilization:true,barnesHut:{gravitationalConstant:-9000,springLength:140,avoidOverlap:0.3}}});
function cnt(a,k){const m={};a.forEach(x=>m[x[k]]=(m[x[k]]||0)+1);return m;}
const nc=cnt(allNodes,'node_type'), ec=cnt(allEdges,'edge_type');
// legend (subsystems/functions only)
if(G.legend&&G.legend.length){document.getElementById('legend').innerHTML=
 G.legend.map(l=>'<label><span class="sw" style="background:'+l.color+'"></span>'+l.sub+'</label>').join('');}
else{document.getElementById('legendGroup').style.display='none';}
// type filters (functions/full only)
if(hasTypes){
 const nf=document.getElementById('nodeF');
 G.node_types.forEach(t=>{const id='nt_'+t;
  nf.insertAdjacentHTML('beforeend','<label><input type="checkbox" id="'+id+'" checked>'+t+'<span class="count">'+(nc[t]||0)+'</span></label>');
  nf.querySelector('#'+CSS.escape(id)).onchange=e=>{e.target.checked?onN.add(t):onN.delete(t);render();};});
 const ef=document.getElementById('edgeF');
 G.edge_types.forEach(t=>{const id='et_'+t; const sw=(EDGE_STYLE[t]||{}).color||'#c3c2b7';
  ef.insertAdjacentHTML('beforeend','<label><input type="checkbox" id="'+id+'" checked><span class="sw" style="background:'+sw+'"></span>'+t+'<span class="count">'+(ec[t]||0)+'</span></label>');
  ef.querySelector('#'+CSS.escape(id)).onchange=e=>{e.target.checked?onE.add(t):onE.delete(t);render();};});
}else{document.getElementById('nodeGroup').style.display='none';document.getElementById('edgeGroup').style.display='none';}
// sub-flow lens (populated from profiles catalogue)
let subflow='';
const sfSel=document.getElementById('subflow');
if(G.profiles&&G.profiles.length){
 G.profiles.forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=p;sfSel.appendChild(o);});
 sfSel.onchange=e=>{subflow=e.target.value;render();};
}else{document.getElementById('subflowGroup').style.display='none';}
// click-to-focus: isolate a node's transitively-connected e2e subgraph
let focusId=null;
const adj={};
allEdges.forEach(e=>{(adj[e.from]=adj[e.from]||[]).push(e.to);(adj[e.to]=adj[e.to]||[]).push(e.from);});
function connected(start){const seen=new Set([start]),stack=[start];
 while(stack.length){const cur=stack.pop();(adj[cur]||[]).forEach(nx=>{if(!seen.has(nx)){seen.add(nx);stack.push(nx);}});}
 return seen;}
net.on('click',params=>{if(params.nodes&&params.nodes.length){focusId=params.nodes[0];}else{focusId=null;}render();});
document.getElementById('clearFocus').onclick=()=>{focusId=null;render();};
function render(){
 const term=(document.getElementById('search').value||'').toLowerCase();
 let vn=allNodes.filter(n=>(!hasTypes||onN.has(n.node_type))&&(!term||(''+(n.label||'')).toLowerCase().includes(term)));
 if(subflow){vn=vn.filter(n=>(n.profiles||[]).includes(subflow));}
 if(focusId!=null){const keep=connected(focusId);vn=vn.filter(n=>keep.has(n.id));}
 const ids=new Set(vn.map(n=>n.id));
 const ve=allEdges.filter(e=>(!hasTypes||onE.has(e.edge_type))&&ids.has(e.from)&&ids.has(e.to));
 nDS.clear();eDS.clear();nDS.add(vn);eDS.add(ve);
 document.getElementById('stats').textContent=vn.length+'/'+allNodes.length+' nodes · '+ve.length+'/'+allEdges.length+' edges'+(focusId!=null?' · focus on #'+focusId:'')+(subflow?' · lens: '+subflow:'');
}
document.getElementById('search').oninput=render;
document.getElementById('fit').onclick=()=>net.fit();
let phys=true;document.getElementById('physics').onclick=()=>{phys=!phys;net.setOptions({physics:phys});};
render(); net.fit();
</script></body></html>"""

_BUILDERS = {
    "subsystems": lambda db, dv: build_subsystems(db),
    "functions": lambda db, dv: build_functions(db, include_data_vars=dv),
    "full": lambda db, dv: build_full(db),
}


def _safe_json(data) -> str:
    """json.dumps hardened for embedding inside a <script> block (PSG-S1).

    json.dumps does NOT escape ``</script>`` or U+2028/U+2029, so a repo-derived
    node name / path containing ``</script>`` would break out of the script tag
    (stored XSS when the viz is opened). Escape the unsafe code points.
    """
    return (json.dumps(data)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace(" ", "\\u2028").replace(" ", "\\u2029"))


def write_html(db_path: str, out_path: str, *, level: str = "full",
               title: str = "state-graph", include_data_vars: bool = False) -> str:
    if level not in _BUILDERS:
        raise ValueError(f"unknown level: {level!r} (use {list(_BUILDERS)})")
    data = _BUILDERS[level](db_path, include_data_vars)
    html = (_TEMPLATE
            .replace("__TITLE__", _html.escape(title))   # PSG-S1: escape title
            .replace("__LEVEL__", _html.escape(level))
            .replace("__GRAPH_DATA__", _safe_json(data)))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path
