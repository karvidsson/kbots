"""agent_graph — render an interactive HTML map of all agents and their setup.

Reads the central roster (team.json — kept in sync with config at startup by
reconcile_roster: tier, model, tools, rights, reports_to, discord), the agent
config (agents*.yaml: provider, privileged flag, denied builtins, extra dirs,
sandbox) and the runtime overrides (agent_config: provider/model/effort), and
renders one self-contained HTML page: the hub at the centre with the others
around it, edges by reports_to, a detail panel on click, and below the graph a
roster card per agent with purpose, harness, rights, reports-to, tools and
skills, machine access and schedules, so nothing needs a click to be seen.
No external assets (works offline / over Discord). The agent then delivers it
with send_discord_file.
"""
# ruff: noqa: E501  — this module embeds a minified HTML/CSS/JS template

import json
import logging
import os
import time
from collections import Counter
from pathlib import Path

from src.core import schedules as sched
from src.core.base import KBOTS_TMP, ToolContext
from src.core.tools import get_all_tools, tool
from src.tools.team import _load_team

logger = logging.getLogger(__name__)

# Provider registry name → what a human calls the harness the agent runs on.
_HARNESS = {
    "claude_code": "Claude Code",
    "codex_cli": "Codex CLI",
    "local": "Local model",
    "mock": "Mock",
}

# One line per tier, from access_control.py, so the page says what a tier
# grants rather than leaving the reader to guess from a colour.
_TIER_MEANS = {
    "privileged": "Full CLI on the machine, every tool, may message any agent.",
    "coordinator": "No CLI, every tool, may message any agent. The fleet hub.",
    "assistant": "No CLI, safe tools only, cannot message other agents.",
}


def _schedule_timing(s: dict) -> str:
    """Human-readable timing for a schedule record (mirrors list_schedules)."""
    from datetime import datetime
    st = s.get("spec_type")
    if st == "cron":
        return f"cron {s['spec']}"
    if st == "every":
        return f"every {int(s['spec']) // 60}min"
    return "once @ " + datetime.fromtimestamp(float(s["spec"])).strftime("%Y-%m-%d %H:%M")


def _config_entries() -> dict[str, dict]:
    """Per-agent config from the overlay's agents*.yaml, or {} without one."""
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    if not overlay:
        return {}
    try:
        from src.core.agent_scaffold import agent_entries
        return agent_entries(Path(overlay))
    except Exception as e:  # a broken config must not take the map down
        logger.debug(f"agent_graph: config entries unavailable: {e}")
        return {}


def _overrides_for(agent_id: str) -> dict[str, str]:
    """Runtime overrides (agent_config) for one agent; {} if unreadable."""
    try:
        from src.tools.agents_admin import _read_overrides
        return _read_overrides(agent_id) or {}
    except Exception as e:
        logger.debug(f"agent_graph: overrides unavailable for {agent_id}: {e}")
        return {}


def _harness(cfg: dict, roster_model: str, overrides: dict) -> dict:
    """Which harness and model the agent actually runs on right now.

    Runtime overrides win over agents.yaml, which wins over the roster copy.
    A provider override with no model override means the provider's default.
    """
    cfg = cfg or {}
    llm = cfg.get("llm") or {}
    provider = overrides.get("provider") or llm.get("provider") or ("claude_code" if roster_model else "")
    if "model" in overrides:
        model = overrides["model"]
    elif overrides.get("provider"):
        model = f"default of {provider}"
    else:
        model = llm.get("model") or roster_model or ""
    return {
        "provider": provider,
        "label": _HARNESS.get(provider, provider or "unknown"),
        "model": model,
        "effort": overrides.get("effort") or cfg.get("effort") or "",
        "overridden": sorted(k for k in ("provider", "model", "effort") if k in overrides),
        "sandbox": llm.get("sandbox", "") if provider == "codex_cli" else "",
    }


def _access(tier: str, cfg: dict) -> dict:
    """What the agent may touch on the machine, beyond the tier word."""
    cfg = cfg or {}
    return {
        "tier": tier,
        "tier_means": _TIER_MEANS.get(tier, ""),
        "privileged_scaffold": bool(cfg.get("privileged")),
        "denied_builtins": list(cfg.get("disallow_builtins") or []),
        "extra_dirs": list(cfg.get("extra_dirs") or []),
    }


def _gather_agents() -> list[dict]:
    """Build node dicts from the roster, enriched from config and overrides."""
    entries = _config_entries()
    try:
        registered_tools = len(get_all_tools())
    except Exception:
        registered_tools = 0
    nodes = []
    for a in _load_team().get("agents", []):
        tools = a.get("tools", "all")
        aid = a.get("id")
        cfg = entries.get(aid) or {}
        overrides = _overrides_for(aid)
        schedules = [
            {"id": s["id"], "timing": _schedule_timing(s), "instruction": s.get("instruction", "")}
            for s in sched.list_schedules(aid) if s.get("enabled")
        ]
        skills = cfg.get("skills", "all")
        tier = a.get("agent_tier", "assistant")
        nodes.append({
            "id": aid,
            "name": a.get("name") or a.get("id"),
            "tier": tier,
            "purpose": a.get("role") or a.get("domain") or cfg.get("description", "") or "",
            "model": a.get("model", ""),
            "tools": tools if isinstance(tools, str) else ", ".join(tools),
            "tool_count": registered_tools if tools == "all" else (len(tools) if isinstance(tools, list) else 0),
            "skills": skills if isinstance(skills, str) else ", ".join(skills),
            "rights": a.get("rights", []),
            "reports_to": a.get("reports_to", ""),
            "discord": a.get("discord", ""),
            "schedules": schedules,
            "harness": _harness(cfg, a.get("model", ""), overrides),
            "access": _access(tier, cfg),
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


# Visual identity: codex/design/visual-identity.md. One accent on a dark
# ground; mono for structure, Inter for prose; the // prefix and the 3px
# left border are the motifs. No webfont link: the page must open offline.
_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
:root{--base:#0a0a0f;--mid:#12121a;--deep:#0d1117;--fg:#fff;--fg2:#aaa;--fg3:#888;--muted:#666;--line:#333;--accent:#ff4444;--accent2:#ff6b6b;--card:rgba(255,255,255,.03);--cardh:rgba(255,68,68,.08)}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--fg);font:14px/1.6 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:linear-gradient(160deg,var(--base) 0%%,var(--mid) 50%%,var(--deep) 100%%)}
.mono{font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{padding:22px 28px 14px;border-bottom:1px solid var(--line)}
.lbl{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-weight:700;font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--accent2)}
.lbl::before{content:"// ";color:var(--accent)}
header h1{margin:4px 0 2px;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:20px;font-weight:700}
header p{margin:0;color:var(--fg2);font-size:13px}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;font-size:12px;color:var(--fg2)}
.legend span::before{content:"";display:inline-block;width:10px;height:10px;border-radius:50%%;margin-right:6px;vertical-align:-1px;border:2px solid var(--c)}
#wrap{display:flex;flex-wrap:wrap;border-bottom:1px solid var(--line)}
#graph{flex:1 1 460px;min-height:56vh;position:relative}
svg{width:100%%;height:100%%;display:block}
.edge{stroke:var(--line);stroke-width:2}
.node{cursor:pointer}.node circle{fill:rgba(255,255,255,.05);stroke-width:3;transition:fill .1s}
.node:hover circle,.node.sel circle{fill:var(--cardh)}
.node text{fill:var(--fg);font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:12px;font-weight:700;text-anchor:middle;pointer-events:none}
.node .sub{fill:var(--fg3);font-size:10px;font-weight:400}
aside{flex:0 0 360px;max-width:100%%;border-left:1px solid var(--line);padding:20px 24px}
aside h2{margin:0;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:17px}
aside .t{color:var(--fg2);font-size:12px;margin:2px 0 14px}
section{margin:14px 0}
section h3{margin:0 0 4px;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-weight:700;font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--accent2)}
section h3::before{content:"// ";color:var(--accent)}
code{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:12px;background:var(--card);border:1px solid var(--line);border-radius:3px;padding:1px 6px;margin:2px 4px 2px 0;display:inline-block;color:var(--fg2)}
.chip{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:11px;padding:2px 8px;border-radius:3px;border:1px solid var(--line);color:var(--fg2);margin-right:6px;white-space:nowrap}
.chip.tier{border-color:var(--c);color:var(--c)}
.chip.h{color:var(--fg)}
.hint{color:var(--fg3)}
.kv{display:grid;grid-template-columns:110px 1fr;gap:4px 12px;font-size:13px}
.kv dt{color:var(--fg3);font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:11px;letter-spacing:.06em;text-transform:uppercase;padding-top:3px}
.kv dd{margin:0}
#roster{padding:22px 28px 40px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:14px;margin-top:12px}
.card{background:var(--card);border-left:3px solid var(--accent);padding:1.1rem 1.5rem;line-height:1.5;cursor:pointer}
.card:hover{background:var(--cardh);border-left-color:var(--accent2)}
.card.sel{border-left-color:var(--accent2);background:var(--cardh)}
.card h4{margin:0 0 2px;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:15px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.card .purpose{color:var(--fg2);margin:6px 0 10px;font-size:13px}
.card .kv{font-size:12.5px}
.warn{color:var(--accent2)}
@media(max-width:700px){aside{flex-basis:100%%;border-left:0;border-top:1px solid var(--line)}}
</style></head><body>
<header>
  <div class="lbl">Agent map</div>
  <h1>%(title)s</h1>
  <p>%(count)d agents · hub <span class="mono">%(hub_name)s</span> · click a node or a card for detail</p>
  <div class="legend"><span style="--c:#ff4444">privileged: full CLI, every tool</span><span style="--c:#ffffff">coordinator: no CLI, every tool, hub</span><span style="--c:#888888">assistant: no CLI, safe tools</span></div>
</header>
<div id="wrap">
  <div id="graph"><svg id="svg"></svg></div>
  <aside id="panel"><p class="hint">Select an agent.</p></aside>
</div>
<div id="roster"><div class="lbl">Roster</div><div class="grid" id="grid"></div></div>
<script>
const NODES = %(nodes)s, HUB = %(hub)s;
const TIER_C = {privileged:"#ff4444", coordinator:"#ffffff", assistant:"#888888"};
const svg = document.getElementById('svg'), panel = document.getElementById('panel'), grid = document.getElementById('grid');
function esc(s){return String(s==null?"":s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function chips(arr){return (arr||[]).length?arr.map(r=>`<code>${esc(r)}</code>`).join(''):'<span class="hint">none</span>';}
function harness(n){const h=n.harness||{};let s=`<b>${esc(h.label||'?')}</b>`;if(h.model)s+=` · <span class="mono">${esc(h.model)}</span>`;if(h.effort)s+=` · effort ${esc(h.effort)}`;if(h.sandbox)s+=` · sandbox ${esc(h.sandbox)}`;if(h.overridden&&h.overridden.length)s+=` <span class="hint">(runtime override: ${esc(h.overridden.join(', '))})</span>`;return s;}
function access(n){const a=n.access||{};const bits=[];bits.push(a.tier_means?esc(a.tier_means):esc(a.tier));if(a.denied_builtins&&a.denied_builtins.length)bits.push(`<span class="warn">denied: ${esc(a.denied_builtins.join(', '))}</span>`);if(a.extra_dirs&&a.extra_dirs.length)bits.push(`also sees: ${a.extra_dirs.map(d=>`<code>${esc(d)}</code>`).join('')}`);return bits.join('<br>');}
function toolsLine(n){let t=n.tools==='all'?`all tools${n.tool_count?` (${n.tool_count} registered)`:''}`:esc(n.tools||'none');let s=n.skills==='all'?'all skills':esc(n.skills||'none');return `${t} · ${s}`;}
function kv(n,full){const rows=[['Harness',harness(n)],['Reports to',n.reports_to?`<span class="mono">${esc(n.reports_to)}</span>`:'<span class="hint">nobody (hub)</span>'],['Access',access(n)],['Tools',toolsLine(n)],['Rights',chips(n.rights)]];
  if(full){rows.push(['Discord ID',n.discord?`<code>${esc(n.discord)}</code>`:'<span class="hint">none</span>']);}
  if(n.schedules&&n.schedules.length)rows.push([`Schedules (${n.schedules.length})`,n.schedules.map(s=>`<div><code>${esc(s.id)}</code> <span class="hint">${esc(s.timing)}</span>${full?'<br>'+esc(s.instruction):''}</div>`).join('')]);
  return `<dl class="kv">${rows.map(([k,v])=>`<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>`;}
function layout(){
  const g=document.getElementById('graph'), W=g.clientWidth||600, H=g.clientHeight||500;
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`); svg.innerHTML='';
  const cx=W/2, cy=H/2, R=Math.max(90,Math.min(W,H)/2-78);
  const others=NODES.filter(n=>n.id!==HUB), hub=NODES.find(n=>n.id===HUB)||NODES[0];
  const pos={}; if(hub)pos[hub.id]=[cx,cy];
  others.forEach((n,i)=>{const a=-Math.PI/2+2*Math.PI*i/others.length; pos[n.id]=[cx+R*Math.cos(a),cy+R*Math.sin(a)];});
  const NS='http://www.w3.org/2000/svg';
  NODES.forEach(n=>{const tgt=(n.reports_to&&pos[n.reports_to])?n.reports_to:(hub&&n.id!==hub.id?hub.id:null);
    if(tgt&&pos[n.id]&&pos[tgt]){const l=document.createElementNS(NS,'line');l.setAttribute('class','edge');
    l.setAttribute('x1',pos[n.id][0]);l.setAttribute('y1',pos[n.id][1]);l.setAttribute('x2',pos[tgt][0]);l.setAttribute('y2',pos[tgt][1]);svg.appendChild(l);}});
  NODES.forEach(n=>{const[x,y]=pos[n.id]||[cx,cy]; const isHub=hub&&n.id===hub.id; const r=isHub?34:26;
    const grp=document.createElementNS(NS,'g');grp.setAttribute('class','node');grp.dataset.id=n.id;grp.setAttribute('transform',`translate(${x},${y})`);
    const c=document.createElementNS(NS,'circle');c.setAttribute('r',r);c.setAttribute('stroke',TIER_C[n.tier]||'#888');grp.appendChild(c);
    const t=document.createElementNS(NS,'text');t.setAttribute('y',r+15);t.textContent=n.name;grp.appendChild(t);
    const tt=document.createElementNS(NS,'text');tt.setAttribute('class','sub');tt.setAttribute('y',r+28);tt.textContent=(n.harness&&n.harness.label)||n.tier;grp.appendChild(tt);
    if(n.schedules&&n.schedules.length){const b=document.createElementNS(NS,'text');b.setAttribute('x',r-4);b.setAttribute('y',-r+4);b.setAttribute('text-anchor','end');b.setAttribute('font-size','13');b.textContent='⏰'+n.schedules.length;grp.appendChild(b);}
    grp.addEventListener('click',()=>select(n));svg.appendChild(grp);});
}
function roster(){grid.innerHTML=NODES.map(n=>`<div class="card" data-id="${esc(n.id)}"><h4>${esc(n.name)} <span class="chip tier" style="--c:${TIER_C[n.tier]||'#888'}">${esc(n.tier)}</span><span class="chip h">${esc((n.harness&&n.harness.label)||'?')}</span></h4><div class="purpose">${esc(n.purpose)||'<span class=hint>no purpose recorded</span>'}</div>${kv(n,false)}</div>`).join('');
  grid.querySelectorAll('.card').forEach(el=>el.addEventListener('click',()=>select(NODES.find(n=>n.id===el.dataset.id))));}
function select(n){
  document.querySelectorAll('.node,.card').forEach(el=>el.classList.toggle('sel',el.dataset.id===n.id));
  panel.innerHTML=`<h2>${esc(n.name)}</h2><div class="t"><span class="chip tier" style="--c:${TIER_C[n.tier]||'#888'}">${esc(n.tier)}</span> <span class="mono">${esc(n.id)}</span></div>`+
    `<section><h3>Purpose</h3><div>${esc(n.purpose)||'<span class=hint>no purpose recorded</span>'}</div></section>`+
    `<section><h3>Setup</h3>${kv(n,true)}</section>`;
}
layout(); roster(); window.addEventListener('resize',layout);
if(NODES.length)select(NODES.find(n=>n.id===HUB)||NODES[0]);
</script></body></html>"""


def _render_html(nodes: list[dict], hub: str, title: str) -> str:
    def safe(obj):
        return json.dumps(obj).replace("</", "<\\/")
    hub_name = next((n["name"] for n in nodes if n["id"] == hub), hub)
    return _HTML % {
        "title": title.replace("<", "&lt;"),
        "count": len(nodes),
        "hub_name": str(hub_name).replace("<", "&lt;"),
        "nodes": safe(nodes),
        "hub": safe(hub),
    }


@tool(
    name="agent_graph",
    description=(
        "Generate an interactive HTML map of all agents — the hub agent at the "
        "centre, others around it, plus a roster card per agent with purpose, "
        "harness (provider and model actually in use), rights, reports-to, "
        "tools and skills, machine access and schedules. Returns a file path; "
        "deliver it to the user with send_discord_file."
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
