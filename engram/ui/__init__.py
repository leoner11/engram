"""Graph browser UI — FastAPI backend with REST API for graph exploration."""

from __future__ import annotations

import json
from pathlib import Path

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.graph.activation import ChangeType
from engram.graph.traversal import GraphTraversal


def create_app(root: Path):
    """Create the FastAPI app for the graph browser."""
    from fastapi import FastAPI, Query
    from fastapi.responses import HTMLResponse, JSONResponse

    db = EngramDB(root)
    store = GraphStore(db)
    traversal = GraphTraversal(store)

    app = FastAPI(title="Engram Graph Browser")

    @app.get("/api/graph")
    def get_graph(file: str | None = None, kind: str | None = None, limit: int = 500):
        all_nodes = store.get_all_nodes()
        nodes_data = []
        for nid, n in all_nodes.items():
            if n.kind == "FILE":
                continue
            if file and n.file_path != file:
                continue
            if kind and n.kind != kind:
                continue
            nodes_data.append({
                "id": n.id, "name": n.name, "kind": n.kind,
                "file_path": n.file_path, "is_exported": n.is_exported,
                "summary": n.summary,
                "line_start": n.line_start, "line_end": n.line_end,
            })
            if len(nodes_data) >= limit:
                break

        edges_data = []
        seen = set()
        for nid in all_nodes:
            for edge in store.get_edges_from(nid):
                key = (edge.source_id, edge.target_id, edge.kind)
                if key not in seen:
                    seen.add(key)
                    edges_data.append({
                        "source": edge.source_id,
                        "target": edge.target_id,
                        "kind": edge.kind,
                    })

        return {"nodes": nodes_data, "edges": edges_data}

    @app.get("/api/node/{node_id:path}")
    def get_node(node_id: str):
        node = store.get_node(node_id)
        if not node:
            return JSONResponse({"error": "Not found"}, 404)

        outgoing = [{"target": e.target_id, "kind": e.kind, "metadata": e.metadata}
                    for e in store.get_edges_from(node_id)]
        incoming = [{"source": e.source_id, "kind": e.kind, "metadata": e.metadata}
                    for e in store.get_edges_to(node_id)]

        # Linked observations
        observations = []
        try:
            rows = store.conn.execute(
                """SELECT o.* FROM observations o
                   JOIN observation_nodes on_ ON on_.observation_id = o.id
                   WHERE on_.node_id = ?""", (node_id,)
            ).fetchall()
            observations = [dict(r) for r in rows]
        except Exception:
            pass

        return {
            "id": node.id, "name": node.name, "kind": node.kind,
            "file_path": node.file_path, "signature": node.signature,
            "docstring": node.docstring, "summary": node.summary,
            "full_source": node.full_source,
            "line_start": node.line_start, "line_end": node.line_end,
            "is_exported": node.is_exported,
            "decorators": node.decorators,
            "outgoing_edges": outgoing,
            "incoming_edges": incoming,
            "observations": observations,
        }

    @app.post("/api/traverse")
    def run_traversal(body: dict):
        seeds = body.get("seeds", [])
        change_types = {ChangeType(ct) for ct in body.get("change_types", ["BODY_MODIFICATION"])}
        max_depth = body.get("max_depth", 2)
        affected = traversal.traverse(seeds, change_types, max_depth)
        return {
            "affected": [
                {"node_id": a.node_id, "depth": a.depth, "priority": a.priority,
                 "reached_via": a.reached_via, "change_types": list(a.change_types)}
                for a in affected
            ]
        }

    @app.get("/api/search")
    def search(q: str = "", type: str | None = None):
        nodes = store.search_nodes_by_name(q) if q else []
        return {
            "nodes": [{"id": n.id, "name": n.name, "kind": n.kind, "file_path": n.file_path}
                      for n in nodes[:20]],
        }

    @app.get("/api/stats")
    def get_stats():
        return store.get_stats()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return GRAPH_UI_HTML

    return app


def run_ui(root: Path, port: int = 8080):
    """Launch the graph browser."""
    import uvicorn
    app = create_app(root)
    print(f"Engram Graph Browser: http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


# Inline HTML UI — lightweight graph explorer
GRAPH_UI_HTML = """<!DOCTYPE html>
<html><head><title>Engram Graph Browser</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system, system-ui, sans-serif; background:#0d1117; color:#c9d1d9; }
.container { display:flex; height:100vh; }
.sidebar { width:350px; border-right:1px solid #30363d; overflow-y:auto; padding:16px; }
.main { flex:1; display:flex; flex-direction:column; }
.toolbar { padding:12px 16px; border-bottom:1px solid #30363d; display:flex; gap:8px; align-items:center; }
.graph-area { flex:1; position:relative; overflow:hidden; }
svg { width:100%; height:100%; }
input, select { background:#21262d; border:1px solid #30363d; color:#c9d1d9; padding:6px 10px; border-radius:6px; font-size:14px; }
input:focus { border-color:#58a6ff; outline:none; }
button { background:#238636; color:white; border:none; padding:6px 14px; border-radius:6px; cursor:pointer; font-size:14px; }
button:hover { background:#2ea043; }
.node-list { list-style:none; }
.node-item { padding:8px; border-radius:6px; cursor:pointer; margin:2px 0; font-size:13px; }
.node-item:hover { background:#161b22; }
.node-item.active { background:#1f6feb22; border:1px solid #1f6feb; }
.badge { display:inline-block; padding:1px 6px; border-radius:10px; font-size:11px; margin-right:4px; }
.badge-func { background:#1f6feb33; color:#58a6ff; }
.badge-class { background:#23863633; color:#3fb950; }
.badge-type { background:#8b5cf633; color:#bc8cff; }
.detail-panel { padding:16px; max-height:50%; overflow-y:auto; border-top:1px solid #30363d; }
.detail-panel h3 { margin-bottom:8px; color:#58a6ff; }
.detail-panel pre { background:#161b22; padding:12px; border-radius:6px; overflow-x:auto; font-size:12px; margin:8px 0; }
h2 { font-size:16px; margin-bottom:12px; color:#58a6ff; }
.stats { color:#8b949e; font-size:13px; margin-bottom:16px; }
</style></head><body>
<div class="container">
  <div class="sidebar">
    <h2>Engram</h2>
    <div class="stats" id="stats">Loading...</div>
    <input type="text" id="search" placeholder="Search nodes..." style="width:100%;margin-bottom:12px;">
    <ul class="node-list" id="nodeList"></ul>
  </div>
  <div class="main">
    <div class="toolbar">
      <select id="filterKind"><option value="">All kinds</option><option>FUNCTION</option><option>CLASS</option><option>TYPE</option></select>
      <select id="changeType">
        <option value="BODY_MODIFICATION">Body Mod</option>
        <option value="SIGNATURE_MODIFICATION">Sig Mod</option>
        <option value="RENAME">Rename</option>
        <option value="FIELD_ADDITION">Field Add</option>
        <option value="DELETION">Deletion</option>
      </select>
      <button onclick="runTraversal()">Traverse from selected</button>
    </div>
    <div class="graph-area"><svg id="graphSvg"></svg></div>
    <div class="detail-panel" id="detail" style="display:none;"></div>
  </div>
</div>
<script>
let allNodes=[], allEdges=[], selectedNode=null, simulation=null;
async function init() {
  const stats = await (await fetch('/api/stats')).json();
  document.getElementById('stats').textContent = `${stats.node_count} nodes, ${stats.edge_count} edges, ${stats.file_count} files`;
  const graph = await (await fetch('/api/graph?limit=200')).json();
  allNodes = graph.nodes; allEdges = graph.edges;
  renderList(allNodes);
  renderGraph();
}
function renderList(nodes) {
  const ul = document.getElementById('nodeList');
  ul.innerHTML = nodes.slice(0,100).map(n => {
    const badge = n.kind === 'FUNCTION' ? 'func' : n.kind === 'CLASS' ? 'class' : 'type';
    return `<li class="node-item" onclick="selectNode('${n.id}')"><span class="badge badge-${badge}">${n.kind[0]}</span>${n.name}</li>`;
  }).join('');
}
async function selectNode(id) {
  selectedNode = id;
  const data = await (await fetch('/api/node/'+encodeURIComponent(id))).json();
  const panel = document.getElementById('detail');
  panel.style.display = 'block';
  panel.innerHTML = `<h3>${data.name}</h3><p>${data.signature||''}</p><p>${data.summary||''}</p>
    <p>In: ${data.incoming_edges.length} edges, Out: ${data.outgoing_edges.length} edges</p>
    <pre>${(data.full_source||'').slice(0,1000)}</pre>`;
  document.querySelectorAll('.node-item').forEach(el => el.classList.remove('active'));
}
async function runTraversal() {
  if (!selectedNode) return alert('Select a node first');
  const ct = document.getElementById('changeType').value;
  const res = await (await fetch('/api/traverse',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({seeds:[selectedNode],change_types:[ct]})})).json();
  const affectedIds = new Set(res.affected.map(a=>a.node_id));
  document.querySelectorAll('circle').forEach(el => {
    el.setAttribute('fill', affectedIds.has(el.dataset.id) ? '#f97316' : kindColor(el.dataset.kind));
    el.setAttribute('r', affectedIds.has(el.dataset.id) ? 8 : 5);
  });
}
function kindColor(k){return k==='FUNCTION'?'#58a6ff':k==='CLASS'?'#3fb950':'#bc8cff';}
function renderGraph(){
  const svg=document.getElementById('graphSvg');
  const w=svg.clientWidth||800,h=svg.clientHeight||600;
  svg.innerHTML=`<g id="edges"></g><g id="nodes"></g>`;
  const nodeMap=Object.fromEntries(allNodes.map(n=>[n.id,{...n,x:Math.random()*w,y:Math.random()*h}]));
  const validEdges=allEdges.filter(e=>nodeMap[e.source]&&nodeMap[e.target]);
  const eg=document.getElementById('edges');
  validEdges.forEach(e=>{const line=document.createElementNS('http://www.w3.org/2000/svg','line');
    line.setAttribute('stroke','#30363d');line.setAttribute('stroke-width','0.5');
    line.dataset.src=e.source;line.dataset.tgt=e.target;eg.appendChild(line);});
  const ng=document.getElementById('nodes');
  Object.values(nodeMap).forEach(n=>{const c=document.createElementNS('http://www.w3.org/2000/svg','circle');
    c.setAttribute('r','5');c.setAttribute('fill',kindColor(n.kind));c.setAttribute('cx',n.x);c.setAttribute('cy',n.y);
    c.dataset.id=n.id;c.dataset.kind=n.kind;c.style.cursor='pointer';
    c.onclick=()=>selectNode(n.id);
    const t=document.createElementNS('http://www.w3.org/2000/svg','text');
    t.setAttribute('x',n.x+8);t.setAttribute('y',n.y+3);t.setAttribute('fill','#8b949e');
    t.setAttribute('font-size','9');t.textContent=n.name.split('.').pop();
    ng.appendChild(c);ng.appendChild(t);});
  // Simple force layout
  for(let i=0;i<50;i++){
    Object.values(nodeMap).forEach(n=>{
      Object.values(nodeMap).forEach(m=>{if(n===m)return;
        const dx=n.x-m.x,dy=n.y-m.y,d=Math.sqrt(dx*dx+dy*dy)||1;
        if(d<80){const f=(80-d)*0.05;n.x+=dx/d*f;n.y+=dy/d*f;}});
      n.x=Math.max(20,Math.min(w-20,n.x));n.y=Math.max(20,Math.min(h-20,n.y));});
    validEdges.forEach(e=>{const s=nodeMap[e.source],t=nodeMap[e.target];if(!s||!t)return;
      const dx=t.x-s.x,dy=t.y-s.y,d=Math.sqrt(dx*dx+dy*dy)||1;
      const f=(d-120)*0.01;s.x+=dx/d*f;s.y+=dy/d*f;t.x-=dx/d*f;t.y-=dy/d*f;});}
  // Update positions
  ng.querySelectorAll('circle').forEach(c=>{const n=nodeMap[c.dataset.id];if(n){c.setAttribute('cx',n.x);c.setAttribute('cy',n.y);}});
  ng.querySelectorAll('text').forEach((t,i)=>{const circles=[...ng.querySelectorAll('circle')];
    if(circles[i]){t.setAttribute('x',+circles[i].getAttribute('cx')+8);t.setAttribute('y',+circles[i].getAttribute('cy')+3);}});
  eg.querySelectorAll('line').forEach(l=>{const s=nodeMap[l.dataset.src],t=nodeMap[l.dataset.tgt];
    if(s&&t){l.setAttribute('x1',s.x);l.setAttribute('y1',s.y);l.setAttribute('x2',t.x);l.setAttribute('y2',t.y);}});
}
document.getElementById('search').oninput=e=>{const q=e.target.value.toLowerCase();
  renderList(allNodes.filter(n=>n.name.toLowerCase().includes(q)||n.id.toLowerCase().includes(q)));};
document.getElementById('filterKind').onchange=e=>{const k=e.target.value;
  renderList(k?allNodes.filter(n=>n.kind===k):allNodes);};
init();
</script></body></html>"""
