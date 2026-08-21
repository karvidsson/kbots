"""Graph memory tools — entity/relationship layer on LadybugDB (src/lib/graph_store).

Additive to the sqlite memory tools: facts go in memory_store, the web of
entities goes here. All writes carry scope + creator so visibility mirrors the
sqlite scope model; that's also why writes only happen through memory_link /
memory_unlink — memory_graph_query is read-only by design.
"""
# ruff: noqa: E501  — memory_graph embeds a minified HTML/CSS/JS template

import json
import logging
import re
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


_D3_URL = "https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"

# Placeholders (__TITLE__ etc.) are substituted with str.replace in
# _render_graph_html so the CSS/JS below can use "%" freely.
_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f7f8fa;--fg:#1a1d21;--muted:#5b6470;--card:#fff;--edge:#c7ccd4;--edge-hl:#4f7cff;--accent:#4f7cff;--dim:.12}
@media(prefers-color-scheme:dark){:root{--bg:#0f1216;--fg:#e6e9ee;--muted:#98a2b3;--card:#171b21;--edge:#3a424e;--edge-hl:#8aa4ff;--accent:#6b8cff}}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg);display:flex;flex-direction:column}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--edge);background:var(--card)}
header h1{margin:0;font-size:16px}header .sub{color:var(--muted);font-size:12px}
header .sp{flex:1}
input[type=search]{font:inherit;font-size:13px;padding:5px 9px;border:1px solid var(--edge);border-radius:6px;background:var(--bg);color:var(--fg);min-width:200px}
button{font:inherit;font-size:12px;padding:5px 9px;border:1px solid var(--edge);border-radius:6px;background:var(--bg);color:var(--fg);cursor:pointer}
button:hover,button.on{border-color:var(--accent);color:var(--accent)}
#wrap{flex:1;display:flex;min-height:0}
#graph{flex:1;position:relative;min-width:0}
svg{width:100%;height:100%;display:block;cursor:grab}svg:active{cursor:grabbing}
.link{stroke:var(--edge);stroke-opacity:.9;fill:none;transition:stroke .15s,stroke-opacity .15s}
.link.hl{stroke:var(--edge-hl);stroke-width:2.2px}
.link.dim{stroke-opacity:var(--dim)}
.elabel{fill:var(--muted);font-size:9px;text-anchor:middle;pointer-events:none;paint-order:stroke;stroke:var(--bg);stroke-width:3px;stroke-linejoin:round}
.elabel.dim{opacity:0}
.node{cursor:pointer}.node circle{stroke:var(--card);stroke-width:1.5px;transition:opacity .15s}
.node.sel circle{stroke:var(--accent);stroke-width:3px}
.node.dim{opacity:var(--dim)}
.node text{fill:var(--fg);font-size:11px;font-weight:600;text-anchor:middle;pointer-events:none;paint-order:stroke;stroke:var(--bg);stroke-width:3px;stroke-linejoin:round}
.node.match circle{stroke:#f5a623;stroke-width:3px}
#legend{position:absolute;left:12px;bottom:12px;background:var(--card);border:1px solid var(--edge);border-radius:8px;padding:8px 10px;font-size:12px;max-width:260px}
#legend div{display:flex;align-items:center;gap:6px;cursor:pointer;padding:1px 0}
#legend div.off{opacity:.4;text-decoration:line-through}
#legend i{width:11px;height:11px;border-radius:50%;display:inline-block}
#nod3{position:absolute;inset:0;display:none;align-items:center;justify-content:center;padding:24px;text-align:center;color:var(--muted)}
aside{flex:0 0 330px;max-width:45%;border-left:1px solid var(--edge);padding:16px 18px;background:var(--card);overflow:auto}
aside h2{margin:0 0 2px;font-size:16px;word-break:break-word}aside .t{color:var(--muted);font-size:12px;margin-bottom:12px;text-transform:uppercase;letter-spacing:.04em}
aside h3{margin:14px 0 4px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
aside .r{margin:5px 0;line-height:1.4}aside .r a{color:var(--fg);text-decoration:none;border-bottom:1px dotted var(--muted);cursor:pointer}aside .r a:hover{color:var(--accent)}
code{background:var(--bg);border:1px solid var(--edge);border-radius:4px;padding:1px 5px;font-size:12px;margin:0 3px;display:inline-block}
.hint{color:var(--muted);font-size:12px}
@media(max-width:760px){#wrap{flex-direction:column}aside{flex:0 0 40%;max-width:none;border-left:0;border-top:1px solid var(--edge)}}
</style></head><body>
<header>
  <div><h1>__TITLE__</h1><div class="sub">__N_NODES__ entities · __N_EDGES__ relationships · drag to move · scroll to zoom · click a node</div></div>
  <span class="sp"></span>
  <input id="q" type="search" placeholder="Search entities…" autocomplete="off">
  <button id="bLabels" class="on" title="Toggle relationship labels">Labels</button>
  <button id="bFreeze" title="Stop / resume the layout simulation">Freeze</button>
  <button id="bFit" title="Fit graph to view">Fit</button>
</header>
<div id="wrap">
  <div id="graph"><svg id="svg"></svg><div id="legend"></div>
    <div id="nod3"><div>Couldn't load the D3 library from the internet.<br>Open this file while online to see the interactive graph.</div></div></div>
  <aside id="panel"><p class="hint">Select an entity to see its relationships.<br><br>Hover highlights the neighbourhood; click pins it. Click a type in the legend to hide it.</p></aside>
</div>
<script src="__D3_URL__"></script>
<script>
const NODES=__NODES__, EDGES=__EDGES__;
(function(){
if(!window.d3){document.getElementById('nod3').style.display='flex';return;}
const svgEl=document.getElementById('svg'), gEl=document.getElementById('graph'), panel=document.getElementById('panel');
const COLORS=['#4f7cff','#30a46c','#e5484d','#f5a623','#9d5bd2','#12a5b0','#d6409f','#7c8a9c'];
const types=[...new Set(NODES.map(n=>n.type||'entity'))];
const color=t=>COLORS[types.indexOf(t)%COLORS.length];
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const byName=new Map(NODES.map(n=>[n.name,n]));
NODES.forEach(n=>{n.type=n.type||'entity';n.degree=0;});
const links=EDGES.filter(e=>byName.has(e.src)&&byName.has(e.dst)).map(e=>({...e,source:e.src,target:e.dst}));
links.forEach(l=>{byName.get(l.src).degree++;byName.get(l.dst).degree++;});
const R=n=>6+Math.min(14,Math.sqrt(n.degree||1)*2.5);
const nbr=new Map(NODES.map(n=>[n.name,new Set([n.name])]));
links.forEach(l=>{nbr.get(l.src).add(l.dst);nbr.get(l.dst).add(l.src);});
const hidden=new Set(); let selected=null, hovered=null, frozen=false, showLabels=true, query='';

const svg=d3.select(svgEl), root=svg.append('g');
svg.append('defs').append('marker').attr('id','arrow').attr('viewBox','0 -4 8 8').attr('refX',8).attr('refY',0)
  .attr('markerWidth',7).attr('markerHeight',7).attr('orient','auto')
  .append('path').attr('d','M0,-4L8,0L0,4').attr('fill','var(--edge)');
const linkG=root.append('g'), labelG=root.append('g'), nodeG=root.append('g');
const zoom=d3.zoom().scaleExtent([.15,6]).on('zoom',ev=>root.attr('transform',ev.transform));
svg.call(zoom).on('dblclick.zoom',null).on('click',()=>{selected=null;paint();panel.innerHTML='<p class="hint">Select an entity to see its relationships.</p>';});

const link=linkG.selectAll('path').data(links).join('path').attr('class','link').attr('stroke-width',l=>1+Math.min(2,(l.confidence||0.5)*1.5)).attr('marker-end','url(#arrow)');
const elabel=labelG.selectAll('text').data(links).join('text').attr('class','elabel').text(l=>l.rel);
const node=nodeG.selectAll('g').data(NODES).join('g').attr('class','node')
  .call(d3.drag().on('start',(ev,d)=>{if(!ev.active&&!frozen)sim.alphaTarget(.3).restart();d.fx=d.x;d.fy=d.y;})
    .on('drag',(ev,d)=>{d.fx=ev.x;d.fy=ev.y;if(frozen)tick();})
    .on('end',(ev,d)=>{if(!ev.active)sim.alphaTarget(0);if(!frozen){d.fx=null;d.fy=null;}}))
  .on('mouseenter',(ev,d)=>{hovered=d;paint();}).on('mouseleave',()=>{hovered=null;paint();})
  .on('click',(ev,d)=>{ev.stopPropagation();select(d);})
  .on('dblclick',(ev,d)=>{ev.stopPropagation();focusOn(d);});
node.append('circle').attr('r',R).attr('fill',d=>color(d.type));
node.append('text').attr('y',d=>R(d)+12).text(d=>d.name.length>28?d.name.slice(0,26)+'…':d.name);
node.append('title').text(d=>d.name+' ('+d.type+')');

const W=()=>gEl.clientWidth||800, H=()=>gEl.clientHeight||600;
const sim=d3.forceSimulation(NODES)
  .force('link',d3.forceLink(links).id(d=>d.name).distance(l=>90+12*Math.min(6,Math.max(l.source.degree,l.target.degree))).strength(.5))
  .force('charge',d3.forceManyBody().strength(d=>-260-25*d.degree))
  .force('collide',d3.forceCollide().radius(d=>R(d)+24))
  .force('center',d3.forceCenter(W()/2,H()/2))
  .force('x',d3.forceX(W()/2).strength(.03)).force('y',d3.forceY(H()/2).strength(.03))
  .on('tick',tick).on('end',()=>{if(!fitted){fit();fitted=true;}});
let fitted=false;
function tick(){
  link.attr('d',l=>{const s=l.source,t=l.target,dx=t.x-s.x,dy=t.y-s.y,d=Math.hypot(dx,dy)||1,tr=R(t)+2;
    return `M${s.x},${s.y}L${t.x-dx/d*tr},${t.y-dy/d*tr}`;});
  elabel.attr('x',l=>(l.source.x+l.target.x)/2).attr('y',l=>(l.source.y+l.target.y)/2-4);
  node.attr('transform',d=>`translate(${d.x},${d.y})`);
}
function fit(){
  const vis=NODES.filter(n=>!hidden.has(n.type)); if(!vis.length)return;
  const xs=vis.map(n=>n.x),ys=vis.map(n=>n.y),x0=Math.min(...xs)-40,x1=Math.max(...xs)+40,y0=Math.min(...ys)-40,y1=Math.max(...ys)+40;
  const k=Math.min(4,.92/Math.max((x1-x0)/W(),(y1-y0)/H()));
  svg.transition().duration(500).call(zoom.transform,d3.zoomIdentity.translate(W()/2-k*(x0+x1)/2,H()/2-k*(y0+y1)/2).scale(k));
}
function paint(){
  const f=selected||hovered, keep=f?nbr.get(f.name):null, q=query.toLowerCase();
  node.classed('sel',d=>d===selected).classed('match',d=>!!q&&d.name.toLowerCase().includes(q))
    .classed('dim',d=>(keep&&!keep.has(d.name)))
    .style('display',d=>hidden.has(d.type)?'none':null);
  const linkVis=l=>!(hidden.has(l.source.type)||hidden.has(l.target.type));
  link.classed('hl',l=>f&&(l.src===f.name||l.dst===f.name)).classed('dim',l=>f&&!(l.src===f.name||l.dst===f.name)).style('display',l=>linkVis(l)?null:'none');
  elabel.classed('dim',l=>!showLabels||(f&&!(l.src===f.name||l.dst===f.name))||(!f&&links.length>80)).style('display',l=>linkVis(l)?null:'none');
}
function select(n){
  selected=n; paint();
  const rel=links.filter(l=>l.src===n.name||l.dst===n.name);
  const out=rel.filter(l=>l.src===n.name), inc=rel.filter(l=>l.dst===n.name);
  const row=(l,o)=>`<div class="r">${o?'':'<a data-n="'+esc(l.src)+'">'+esc(l.src)+'</a>'}<code>${esc(l.rel)}</code>${o?'<a data-n="'+esc(l.dst)+'">'+esc(l.dst)+'</a>':''}`+
    `<div class="hint">conf ${Number(l.confidence).toFixed(2)} · ${esc(l.scope)}${l.created_by?' · by '+esc(l.created_by):''}</div></div>`;
  panel.innerHTML=`<h2>${esc(n.name)}</h2><div class="t"><i style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${color(n.type)};margin-right:5px"></i>${esc(n.type)} · ${rel.length} relationship(s)</div>`+
    (out.length?`<h3>Outgoing (${out.length})</h3>`+out.map(l=>row(l,true)).join(''):'')+
    (inc.length?`<h3>Incoming (${inc.length})</h3>`+inc.map(l=>row(l,false)).join(''):'')+
    (rel.length?'':'<p class="hint">No relationships.</p>')+
    `<p class="hint" style="margin-top:14px">Double-click the node to centre on it.</p>`;
  panel.querySelectorAll('a[data-n]').forEach(a=>a.onclick=()=>{const m=byName.get(a.dataset.n);if(m){select(m);focusOn(m);}});
}
function focusOn(n){
  const k=Math.max(1.2,d3.zoomTransform(svgEl).k);
  svg.transition().duration(450).call(zoom.transform,d3.zoomIdentity.translate(W()/2-k*n.x,H()/2-k*n.y).scale(k));
}
// legend
const legend=d3.select('#legend');
legend.selectAll('div').data(types).join('div').html(t=>`<i style="background:${color(t)}"></i>${esc(t)} <span class="hint">(${NODES.filter(n=>n.type===t).length})</span>`)
  .on('click',(ev,t)=>{hidden.has(t)?hidden.delete(t):hidden.add(t);legend.selectAll('div').classed('off',x=>hidden.has(x));paint();});
// controls
const q=document.getElementById('q');
q.addEventListener('input',()=>{query=q.value.trim();paint();
  if(query){const m=NODES.find(n=>!hidden.has(n.type)&&n.name.toLowerCase().includes(query.toLowerCase()));if(m)focusOn(m);}});
q.addEventListener('keydown',ev=>{if(ev.key==='Enter'){const m=NODES.find(n=>n.name.toLowerCase().includes(query.toLowerCase()));if(m)select(m);}
  if(ev.key==='Escape'){q.value='';query='';paint();}});
document.getElementById('bLabels').onclick=function(){showLabels=!showLabels;this.classList.toggle('on',showLabels);paint();};
document.getElementById('bFreeze').onclick=function(){frozen=!frozen;this.classList.toggle('on',frozen);
  if(frozen){sim.stop();NODES.forEach(n=>{n.fx=n.x;n.fy=n.y;});}else{NODES.forEach(n=>{n.fx=null;n.fy=null;});sim.alpha(.5).restart();}};
document.getElementById('bFit').onclick=fit;
window.addEventListener('resize',()=>{sim.force('center',d3.forceCenter(W()/2,H()/2));if(!frozen)sim.alpha(.2).restart();});
document.addEventListener('keydown',ev=>{if(ev.key==='/'&&document.activeElement!==q){ev.preventDefault();q.focus();}});
paint();
})();
</script></body></html>"""


def _render_graph_html(nodes: list[dict], edges: list[dict], title: str) -> str:
    def safe(obj):
        return json.dumps(obj).replace("</", "<\\/")
    subs = {
        "__TITLE__": title.replace("&", "&amp;").replace("<", "&lt;"),
        "__N_NODES__": str(len(nodes)),
        "__N_EDGES__": str(len(edges)),
        "__D3_URL__": _D3_URL,
        "__NODES__": safe(nodes),
        "__EDGES__": safe(edges),
    }
    # Single pass so substituted content (e.g. a title) is never re-scanned.
    return re.sub("|".join(subs), lambda m: subs[m.group(0)], _HTML)


def _may_view_all(agent_id: str) -> bool:
    """Fleet view is for the coordinator / privileged agents only — it reveals
    every agent's private edges."""
    import os

    from src.core.agent_scaffold import agent_is_privileged_or_coordinator
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    return bool(overlay) and agent_is_privileged_or_coordinator(Path(overlay), agent_id)


@tool(
    name="memory_graph",
    description=(
        "Render the memory graph as an interactive HTML file (D3 force "
        "layout: zoom, drag, search, neighbourhood highlighting; needs internet "
        "to load D3). Default shows only your own memories; view='shared' adds the "
        "global/group edges other agents contributed; view='all' is the fleet-wide "
        "map of every agent's edges (coordinator/privileged only). Returns a file "
        "path; deliver it to the user with send_discord_file."
    ),
    category="memory",
)
async def memory_graph(ctx: ToolContext, title: str = "Memory Graph", view: str = "own") -> str:
    """Render the memory graph as an interactive HTML file.

    Args:
        title: Heading shown on the page
        view: 'own' (default) — only this agent's memories (edges it created or
              scoped to it); 'shared' — everything visible to it, including
              global/group edges from other agents; 'all' — every edge from
              every agent regardless of scope (coordinator/privileged only)
    """
    if view not in ("own", "shared", "all"):
        return "Invalid view — use 'own' (default), 'shared', or 'all'."
    if view == "all" and not _may_view_all(ctx.agent_id):
        return ("view='all' shows every agent's private edges — only the "
                "coordinator or a privileged agent may use it. Use 'shared' "
                "for everything visible to you.")
    try:
        data = await get_graph().export(agent_id=ctx.agent_id,
                                        own_only=(view == "own"),
                                        all_scopes=(view == "all"))
    except GraphUnavailableError as e:
        return str(e)
    if not data["edges"]:
        if view == "own":
            return ("You have no memories in the graph yet — nothing to visualize. "
                    "Store relationships with memory_link first.")
        return "The memory graph is empty — nothing to visualize yet. Store relationships with memory_link first."
    if view == "all":
        # Colour the fleet view by owner: node type becomes the agent whose
        # edges touch it ('shared' for global/group edges, 'mixed' when several
        # agents touch the same entity), so the legend doubles as a per-agent
        # breakdown with click-to-hide.
        owners: dict[str, set] = {}
        for e in data["edges"]:
            scope = e.get("scope") or ""
            owner = scope.removeprefix("agent:") if scope.startswith("agent:") else "shared"
            for name in (e["src"], e["dst"]):
                owners.setdefault(name, set()).add(owner)
        for n in data["nodes"]:
            o = owners.get(n["name"], set())
            if o:
                n["type"] = next(iter(o)) if len(o) == 1 else "mixed"
    doc = _render_graph_html(data["nodes"], data["edges"], title)
    out_dir = Path(ctx.project_dir) if ctx.project_dir else KBOTS_TMP
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"memory-graph-{int(time.time())}.html"
    out.write_text(doc)
    logger.info(f"memory_graph: {len(data['nodes'])} nodes, {len(data['edges'])} edges → {out}")
    return (f"Memory graph generated: {out}\n"
            f"({len(data['nodes'])} entities, {len(data['edges'])} relationships). "
            f"Send it to the user now with send_discord_file(channel_id, \"{out}\").")
