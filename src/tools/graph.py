"""Graph memory tools — entity/relationship layer on LadybugDB (src/lib/graph_store).

Additive to the sqlite memory tools: facts go in memory_store, the web of
entities goes here. All writes carry scope + creator so visibility mirrors the
sqlite scope model; that's also why writes only happen through memory_link /
memory_unlink — memory_graph_query is read-only by design.
"""
# ruff: noqa: E501  — memory_graph embeds a minified HTML/CSS/JS template

import json
import logging
import time
from pathlib import Path

from src.core.base import KBOTS_TMP, ToolContext
from src.core.tools import tool
from src.lib.graph_store import GraphUnavailableError, get_graph

logger = logging.getLogger(__name__)


@tool(name="memory_link", description="Link two entities in graph memory (a —rel→ b)", category="memory")
async def memory_link(
    ctx: ToolContext,
    a: str,
    rel: str,
    b: str,
    confidence: float = 0.7,
    scope: str = "agent",
) -> str:
    """Store a relationship between two entities in the shared memory graph.

    Args:
        a: Source entity name (e.g. a person, project, place, concept)
        rel: The relationship, short and reusable (e.g. works_at, part_of, uses)
        b: Target entity name
        confidence: How sure you are (0.0-1.0)
        scope: Visibility — agent (private to you), global, or group:<name>
    """
    try:
        edge = await get_graph().link(a, rel, b, confidence=confidence,
                                      scope=scope, created_by=ctx.agent_id)
    except (GraphUnavailableError, ValueError) as e:
        return str(e)
    return f"Linked: {edge['a']} —{edge['rel']}→ {edge['b']} (confidence {edge['confidence']}, scope {edge['scope']})"


@tool(name="memory_related", description="Find entities related to an entity (graph traversal)", category="memory")
async def memory_related(ctx: ToolContext, entity: str, depth: int = 1, limit: int = 25) -> str:
    """Traverse the memory graph from an entity and list its connections.

    Args:
        entity: Entity name to start from
        depth: How many hops to follow (1-3)
        limit: Max edges to return
    """
    try:
        edges = await get_graph().related(entity, depth=depth, agent_id=ctx.agent_id, limit=limit)
    except GraphUnavailableError as e:
        return str(e)
    if not edges:
        return f"No relationships found for '{entity}'."
    lines = [f"Relationships around '{entity}' (depth {min(max(int(depth), 1), 3)}):"]
    current_hop = 0
    for e in edges:
        if e["hop"] != current_hop:
            current_hop = e["hop"]
            lines.append(f" hop {current_hop}:")
        lines.append(f"  {e['src']} —{e['rel']}→ {e['dst']} (conf {e['confidence']:.2f}, {e['scope']})")
    return "\n".join(lines)


@tool(name="memory_unlink", description="Remove a relationship you created from graph memory", category="memory")
async def memory_unlink(ctx: ToolContext, a: str, rel: str, b: str) -> str:
    """Remove a relationship. Only edges you created (or scoped to you) can be removed."""
    try:
        n = await get_graph().unlink(a, rel, b, agent_id=ctx.agent_id)
    except GraphUnavailableError as e:
        return str(e)
    if n == 0:
        return (f"No removable edge {a} —{rel}→ {b} found "
                "(it may not exist, or it belongs to another agent).")
    return f"Removed: {a} —{rel}→ {b}"


@tool(name="memory_graph_query", description="Search graph memory by relationship and/or entity", category="memory")
async def memory_graph_query(ctx: ToolContext, rel: str = "", entity: str = "", limit: int = 50) -> str:
    """Search the memory graph for edges, scoped to what you're allowed to see.

    This is a structured, scope-enforced search — not raw Cypher — so it can
    only ever return edges visible to you (global, your group, your own private).
    Give a relationship, an entity, both, or neither (to list everything visible).

    Args:
        rel: Filter to this relationship, e.g. works_at (optional)
        entity: Filter to edges touching this entity (optional)
        limit: Max rows to return
    """
    try:
        rows = await get_graph().find(rel=rel or None, entity=entity or None,
                                      agent_id=ctx.agent_id, limit=limit)
    except GraphUnavailableError as e:
        return str(e)
    if not rows:
        return "No matching relationships found."
    lines = [f"{len(rows)} relationship(s):"]
    lines += [f"  {r['src']} —{r['rel']}→ {r['dst']} (conf {r['confidence']:.2f}, {r['scope']})"
              for r in rows]
    return "\n".join(lines)


_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
:root{--bg:#f7f8fa;--fg:#1a1d21;--muted:#5b6470;--card:#fff;--edge:#c7ccd4;--accent:#4f7cff}
@media(prefers-color-scheme:dark){:root{--bg:#0f1216;--fg:#e6e9ee;--muted:#98a2b3;--card:#171b21;--edge:#333a44;--accent:#6b8cff}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:14px 20px;border-bottom:1px solid var(--edge)}header h1{margin:0;font-size:17px}
header p{margin:2px 0 0;color:var(--muted);font-size:13px}
#wrap{display:flex;flex-wrap:wrap}#graph{flex:1 1 460px;min-height:70vh;position:relative}
svg{width:100%%;height:100%%;display:block}
.edge{stroke:var(--edge);stroke-width:1.5}.edge.hl{stroke:var(--accent);stroke-width:2.5}
.elabel{fill:var(--muted);font-size:10px;text-anchor:middle;pointer-events:none}
.node{cursor:pointer}.node circle{stroke:var(--card);stroke-width:2}
.node:hover circle,.node.sel circle{stroke:var(--accent);stroke-width:3}
.node text{fill:var(--fg);font-size:11px;font-weight:600;text-anchor:middle;pointer-events:none}
aside{flex:0 0 320px;max-width:100%%;border-left:1px solid var(--edge);padding:18px 20px;background:var(--card)}
aside h2{margin:0 0 2px;font-size:16px}aside .t{color:var(--muted);font-size:12px;margin-bottom:14px;text-transform:uppercase;letter-spacing:.04em}
aside section{margin:14px 0}aside h3{margin:0 0 4px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
code{background:var(--bg);border:1px solid var(--edge);border-radius:4px;padding:1px 5px;font-size:12px;margin:2px 3px 2px 0;display:inline-block}
.hint{color:var(--muted)}
</style></head><body>
<header><h1>%(title)s</h1><p>%(n_nodes)d entities · %(n_edges)d relationships · click a node</p></header>
<div id="wrap">
  <div id="graph"><svg id="svg"></svg></div>
  <aside id="panel"><p class="hint">Select an entity to see its relationships.</p></aside>
</div>
<script>
const NODES=%(nodes)s, EDGES=%(edges)s;
const svg=document.getElementById('svg'), panel=document.getElementById('panel'), NS='http://www.w3.org/2000/svg';
const COLORS=['#4f7cff','#30a46c','#e5484d','#f5a623','#9d5bd2','#12a5b0','#d6409f'];
const types=[...new Set(NODES.map(n=>n.type))];
const color=n=>COLORS[types.indexOf(n.type)%%COLORS.length];
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
const idx={}; NODES.forEach((n,i)=>idx[n.name]=i);
function layout(W,H){
  // simple force simulation: spring edges, pairwise repulsion, center gravity
  const N=NODES.length; if(!N)return;
  NODES.forEach((n,i)=>{const a=2*Math.PI*i/N, r=Math.min(W,H)/3;
    n.x=W/2+r*Math.cos(a)+(i%%7)*3; n.y=H/2+r*Math.sin(a)+(i%%5)*3;});
  const K=60;
  for(let it=0;it<250;it++){
    const fx=new Array(N).fill(0), fy=new Array(N).fill(0);
    for(let i=0;i<N;i++)for(let j=i+1;j<N;j++){
      let dx=NODES[i].x-NODES[j].x, dy=NODES[i].y-NODES[j].y;
      let d2=dx*dx+dy*dy||1, d=Math.sqrt(d2), f=K*K/d2*8;
      dx/=d;dy/=d; fx[i]+=dx*f;fy[i]+=dy*f;fx[j]-=dx*f;fy[j]-=dy*f;}
    EDGES.forEach(e=>{const i=idx[e.src], j=idx[e.dst]; if(i==null||j==null)return;
      let dx=NODES[i].x-NODES[j].x, dy=NODES[i].y-NODES[j].y;
      let d=Math.sqrt(dx*dx+dy*dy)||1, f=(d-K*1.6)*0.05;
      dx/=d;dy/=d; fx[i]-=dx*f*d/8;fy[i]-=dy*f*d/8;fx[j]+=dx*f*d/8;fy[j]+=dy*f*d/8;});
    const cool=1-it/250;
    NODES.forEach((n,i)=>{
      fx[i]+=(W/2-n.x)*0.02; fy[i]+=(H/2-n.y)*0.02;
      n.x+=Math.max(-12,Math.min(12,fx[i]*cool)); n.y+=Math.max(-12,Math.min(12,fy[i]*cool));
      n.x=Math.max(30,Math.min(W-30,n.x)); n.y=Math.max(30,Math.min(H-30,n.y));});
  }
}
function render(){
  const g=document.getElementById('graph'), W=g.clientWidth||700, H=Math.max(g.clientHeight,500);
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`); svg.innerHTML=''; layout(W,H);
  EDGES.forEach(e=>{const s=NODES[idx[e.src]], t=NODES[idx[e.dst]]; if(!s||!t)return;
    const l=document.createElementNS(NS,'line'); l.setAttribute('class','edge'); e._el=l;
    l.setAttribute('x1',s.x);l.setAttribute('y1',s.y);l.setAttribute('x2',t.x);l.setAttribute('y2',t.y);svg.appendChild(l);
    if(EDGES.length<=80){const t2=document.createElementNS(NS,'text');t2.setAttribute('class','elabel');
      t2.setAttribute('x',(s.x+t.x)/2);t2.setAttribute('y',(s.y+t.y)/2-3);t2.textContent=e.rel;svg.appendChild(t2);}});
  NODES.forEach(n=>{
    const grp=document.createElementNS(NS,'g');grp.setAttribute('class','node');n._el=grp;
    grp.setAttribute('transform',`translate(${n.x},${n.y})`);
    const r=8+Math.min(12,Math.sqrt(n.degree||1)*3);
    const c=document.createElementNS(NS,'circle');c.setAttribute('r',r);c.setAttribute('fill',color(n));grp.appendChild(c);
    const t=document.createElementNS(NS,'text');t.setAttribute('y',r+13);t.textContent=n.name;grp.appendChild(t);
    grp.addEventListener('click',()=>select(n));svg.appendChild(grp);});
}
function select(n){
  NODES.forEach(m=>m._el&&m._el.classList.toggle('sel',m===n));
  EDGES.forEach(e=>e._el&&e._el.classList.toggle('hl',e.src===n.name||e.dst===n.name));
  const rel=EDGES.filter(e=>e.src===n.name||e.dst===n.name);
  panel.innerHTML=`<h2>${esc(n.name)}</h2><div class="t">${esc(n.type)} · ${rel.length} relationship(s)</div>`+
    `<section><h3>Relationships</h3><div>`+
    (rel.map(e=>{const out=e.src===n.name;
      return `<div style="margin:4px 0">${out?'':esc(e.src)+' '}<code>${out?esc(e.rel):'←'+esc(e.rel)}</code> ${out?esc(e.dst):''}`+
        `<span class="hint"> · conf ${Number(e.confidence).toFixed(2)} · ${esc(e.scope)}${e.created_by?' · by '+esc(e.created_by):''}</span></div>`;}).join('')||'<span class="hint">—</span>')+
    `</div></section>`;
}
render(); window.addEventListener('resize',render);
</script></body></html>"""


def _render_graph_html(nodes: list[dict], edges: list[dict], title: str) -> str:
    def safe(obj):
        return json.dumps(obj).replace("</", "<\\/")
    return _HTML % {
        "title": title.replace("<", "&lt;"),
        "n_nodes": len(nodes),
        "n_edges": len(edges),
        "nodes": safe(nodes),
        "edges": safe(edges),
    }


@tool(
    name="memory_graph",
    description=(
        "Render this agent's memory graph as a self-contained interactive HTML "
        "file. Default shows only your own memories; view='shared' adds the "
        "global/group edges other agents contributed. Returns a file path; "
        "deliver it to the user with send_discord_file."
    ),
    category="memory",
)
async def memory_graph(ctx: ToolContext, title: str = "Memory Graph", view: str = "own") -> str:
    """Render the memory graph as an interactive HTML file.

    Args:
        title: Heading shown on the page
        view: 'own' (default) — only this agent's memories (edges it created or
              scoped to it); 'shared' — everything visible to it, including
              global/group edges from other agents
    """
    if view not in ("own", "shared"):
        return "Invalid view — use 'own' (default) or 'shared'."
    try:
        data = await get_graph().export(agent_id=ctx.agent_id, own_only=(view == "own"))
    except GraphUnavailableError as e:
        return str(e)
    if not data["edges"]:
        if view == "own":
            return ("You have no memories in the graph yet — nothing to visualize. "
                    "Store relationships with memory_link first.")
        return "The memory graph is empty — nothing to visualize yet. Store relationships with memory_link first."
    doc = _render_graph_html(data["nodes"], data["edges"], title)
    out_dir = Path(ctx.project_dir) if ctx.project_dir else KBOTS_TMP
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"memory-graph-{int(time.time())}.html"
    out.write_text(doc)
    logger.info(f"memory_graph: {len(data['nodes'])} nodes, {len(data['edges'])} edges → {out}")
    return (f"Memory graph generated: {out}\n"
            f"({len(data['nodes'])} entities, {len(data['edges'])} relationships). "
            f"Send it to the user now with send_discord_file(channel_id, \"{out}\").")
