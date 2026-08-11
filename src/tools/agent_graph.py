"""agent_graph — render an interactive HTML map of all agents and their setup.

Reads the central roster (team.json — kept in sync with config at startup by
reconcile_roster: tier, model, tools, rights, reports_to, discord) and renders a
self-contained interactive HTML file: the hub (coordinator / main agent) at the
centre, other agents as circles around it, edges by reports_to. Clicking a node
shows its purpose, skills, and rights. No external assets (works offline / over
Discord). The agent then delivers it with send_discord_file.
"""
# ruff: noqa: E501  — this module embeds a minified HTML/CSS/JS template

import json
import logging
import time
from collections import Counter
from pathlib import Path

from src.core import schedules as sched
from src.core.base import KBOTS_TMP, ToolContext
from src.core.tools import tool
from src.tools.team import _load_team

logger = logging.getLogger(__name__)


def _schedule_timing(s: dict) -> str:
    """Human-readable timing for a schedule record (mirrors list_schedules)."""
    from datetime import datetime
    st = s.get("spec_type")
    if st == "cron":
        return f"cron {s['spec']}"
    if st == "every":
        return f"every {int(s['spec']) // 60}min"
    return "once @ " + datetime.fromtimestamp(float(s["spec"])).strftime("%Y-%m-%d %H:%M")


def _gather_agents() -> list[dict]:
    """Build node dicts from the central roster (team.json), which main.py keeps in
    sync with config (tier/model/tools/rights/reports_to) and prunes at startup."""
    nodes = []
    for a in _load_team().get("agents", []):
        tools = a.get("tools", "all")
        aid = a.get("id")
        schedules = [
            {"id": s["id"], "timing": _schedule_timing(s), "instruction": s.get("instruction", "")}
            for s in sched.list_schedules(aid) if s.get("enabled")
        ]
        nodes.append({
            "id": aid,
            "name": a.get("name") or a.get("id"),
            "tier": a.get("agent_tier", "assistant"),
            "purpose": a.get("role") or a.get("domain") or "",
            "model": a.get("model", ""),
            "tools": tools if isinstance(tools, str) else ", ".join(tools),
            "rights": a.get("rights", []),
            "reports_to": a.get("reports_to", ""),
            "discord": a.get("discord", ""),
            "schedules": schedules,
        })
    return nodes


def _hub_id(nodes: list[dict]) -> str:
    for n in nodes:
        if n["tier"] == "coordinator":
            return n["id"]
    counts = Counter(n["reports_to"] for n in nodes if n["reports_to"])
    if counts:
        return counts.most_common(1)[0][0]
    return nodes[0]["id"] if nodes else ""


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
#wrap{display:flex;flex-wrap:wrap}#graph{flex:1 1 420px;min-height:60vh;position:relative}
svg{width:100%%;height:100%%;display:block}
.edge{stroke:var(--edge);stroke-width:2}
.node{cursor:pointer}.node circle{stroke:var(--card);stroke-width:3;transition:r .1s}
.node:hover circle{stroke:var(--accent)}.node text{fill:var(--fg);font-size:12px;font-weight:600;text-anchor:middle;pointer-events:none}
.node .tier{fill:var(--muted);font-size:10px;font-weight:400}
aside{flex:0 0 320px;max-width:100%%;border-left:1px solid var(--edge);padding:18px 20px;background:var(--card)}
aside h2{margin:0 0 2px;font-size:16px}aside .t{color:var(--muted);font-size:12px;margin-bottom:14px;text-transform:uppercase;letter-spacing:.04em}
aside section{margin:14px 0}aside h3{margin:0 0 4px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
code{background:var(--bg);border:1px solid var(--edge);border-radius:4px;padding:1px 5px;font-size:12px;margin:2px 3px 2px 0;display:inline-block}
.hint{color:var(--muted)}
</style></head><body>
<header><h1>%(title)s</h1><p>%(count)d agents · click a node for details</p></header>
<div id="wrap">
  <div id="graph"><svg id="svg"></svg></div>
  <aside id="panel"><p class="hint">Select an agent node to see its purpose, skills, and rights.</p></aside>
</div>
<script>
const NODES = %(nodes)s, HUB = %(hub)s;
const TIER_COLORS = {privileged:"#e5484d", coordinator:"#4f7cff", assistant:"#30a46c"};
const svg = document.getElementById('svg'), panel = document.getElementById('panel');
function esc(s){return String(s==null?"":s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function layout(){
  const g=document.getElementById('graph'), W=g.clientWidth||600, H=g.clientHeight||500;
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`); svg.innerHTML='';
  const cx=W/2, cy=H/2, R=Math.max(90,Math.min(W,H)/2-70);
  const others=NODES.filter(n=>n.id!==HUB), hub=NODES.find(n=>n.id===HUB)||NODES[0];
  const pos={}; if(hub)pos[hub.id]=[cx,cy];
  others.forEach((n,i)=>{const a=-Math.PI/2+2*Math.PI*i/others.length; pos[n.id]=[cx+R*Math.cos(a),cy+R*Math.sin(a)];});
  const NS='http://www.w3.org/2000/svg';
  // edges: each node -> its reports_to (or -> hub)
  NODES.forEach(n=>{const tgt=(n.reports_to&&pos[n.reports_to])?n.reports_to:(hub&&n.id!==hub.id?hub.id:null);
    if(tgt&&pos[n.id]&&pos[tgt]){const l=document.createElementNS(NS,'line');l.setAttribute('class','edge');
    l.setAttribute('x1',pos[n.id][0]);l.setAttribute('y1',pos[n.id][1]);l.setAttribute('x2',pos[tgt][0]);l.setAttribute('y2',pos[tgt][1]);svg.appendChild(l);}});
  // nodes
  NODES.forEach(n=>{const[x,y]=pos[n.id]||[cx,cy]; const isHub=hub&&n.id===hub.id; const r=isHub?34:26;
    const grp=document.createElementNS(NS,'g');grp.setAttribute('class','node');grp.setAttribute('transform',`translate(${x},${y})`);
    const c=document.createElementNS(NS,'circle');c.setAttribute('r',r);c.setAttribute('fill',TIER_COLORS[n.tier]||'#888');grp.appendChild(c);
    const t=document.createElementNS(NS,'text');t.setAttribute('y',r+15);t.textContent=n.name;grp.appendChild(t);
    const tt=document.createElementNS(NS,'text');tt.setAttribute('class','tier');tt.setAttribute('y',r+29);tt.textContent=n.tier;grp.appendChild(tt);
    if(n.schedules&&n.schedules.length){const b=document.createElementNS(NS,'text');b.setAttribute('x',r-4);b.setAttribute('y',-r+4);b.setAttribute('text-anchor','end');b.setAttribute('font-size','13');b.textContent='⏰'+n.schedules.length;grp.appendChild(b);}
    grp.addEventListener('click',()=>select(n));svg.appendChild(grp);});
}
function select(n){
  const rights=(n.rights||[]).map(r=>`<code>${esc(r)}</code>`).join('')||'<span class="hint">—</span>';
  const tools=n.tools==='all'?'all tools':esc(n.tools||'—');
  panel.innerHTML=`<h2>${esc(n.name)}</h2><div class="t">${esc(n.tier)}${n.model?' · '+esc(n.model):''}</div>`+
    `<section><h3>Purpose</h3><div>${esc(n.purpose)||'<span class=hint>—</span>'}</div></section>`+
    (n.discord?`<section><h3>Discord ID</h3><div><code>${esc(n.discord)}</code></div></section>`:'')+
    `<section><h3>Skills / tools</h3><div>${tools}</div></section>`+
    `<section><h3>Rights</h3><div>${rights}</div></section>`+
    (n.reports_to?`<section><h3>Reports to</h3><div>${esc(n.reports_to)}</div></section>`:'')+
    ((n.schedules&&n.schedules.length)?`<section><h3>Scheduled tasks (${n.schedules.length})</h3><div>`+n.schedules.map(s=>`<div style="margin:4px 0"><code>${esc(s.id)}</code> <span class="hint">${esc(s.timing)}</span><br>${esc(s.instruction)}</div>`).join('')+`</div></section>`:'');
}
layout(); window.addEventListener('resize',layout);
if(NODES.length)select(NODES.find(n=>n.id===HUB)||NODES[0]);
</script></body></html>"""


def _render_html(nodes: list[dict], hub: str, title: str) -> str:
    def safe(obj):
        return json.dumps(obj).replace("</", "<\\/")
    return _HTML % {
        "title": title.replace("<", "&lt;"),
        "count": len(nodes),
        "nodes": safe(nodes),
        "hub": safe(hub),
    }


@tool(
    name="agent_graph",
    description=(
        "Generate an interactive HTML map of all agents — the hub agent at the "
        "centre, others around it, each node showing purpose, skills, and rights. "
        "Returns a file path; deliver it to the user with send_discord_file."
    ),
    category="team",
)
async def agent_graph(ctx: ToolContext, title: str = "Agent Map") -> str:
    nodes = _gather_agents()
    if not nodes:
        return "No agents found to graph."
    hub = _hub_id(nodes)
    doc = _render_html(nodes, hub, title)
    out_dir = Path(ctx.project_dir) if ctx.project_dir else KBOTS_TMP
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"agent-graph-{int(time.time())}.html"
    out.write_text(doc)
    logger.info(f"agent_graph: {len(nodes)} agents, hub={hub} → {out}")
    return (f"Agent map generated: {out}\n"
            f"({len(nodes)} agents; hub = {hub}). "
            f"Send it to the user now with send_discord_file(channel_id, \"{out}\").")
