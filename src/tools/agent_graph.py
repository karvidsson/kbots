"""agent_graph — render an interactive HTML map of all agents and their setup.

Reads the central roster (team.json — kept in sync with config at startup by
reconcile_roster: tier, model, tools, rights, reports_to, discord), the agent
config (agents*.yaml: provider, privileged flag, denied builtins, extra dirs,
sandbox, project_dir), the runtime overrides (agent_config: provider/model/
effort) and each agent's avatar, and renders one self-contained HTML page:
the hub at the centre with the others around it, avatars in the nodes, edges
by reports_to, a detail panel on click, and below the graph a roster card per
agent. Each card opens on purpose and a one-line summary; harness, access,
tools, rights and schedules sit in accordion panes so the fleet is scannable
and any agent can be opened in full. Rights are summarised by what they grant
(files, shell, web, MCP) rather than listed as raw rules. No external assets
(works offline / over Discord). The agent then delivers it with
send_discord_file.
"""
# ruff: noqa: E501  — this module embeds a minified HTML/CSS/JS template

import base64
import io
import json
import logging
import os
import re
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

# Rights rules are "Verb(pattern)". These verbs all mean "files", and the
# reader wants the paths, not six copies of the same path under six verbs.
_FILE_VERBS = ("Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "NotebookEdit")
_WEB_VERBS = ("WebSearch", "WebFetch")
_AVATAR_PX = 96            # inline size; ~5 KB per agent as PNG, ~600 B as SVG
_AVATAR_MAX_BYTES = 120_000  # a PNG we cannot shrink is skipped past this


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


_RULE = re.compile(r"^([A-Za-z0-9_\-*]+)(?:\((.*)\))?$")


def _summarize_rights(rights: list[str]) -> dict:
    """Group raw permission rules by what they grant.

    Six file verbs over the same path collapse to one path entry; Bash rules
    become a shell allow-list (or "everything" for Bash(*)); mcp__ entries
    are the MCP servers. The raw list is kept for the detail pane.
    """
    files: list[str] = []
    shell: list[str] = []
    web: list[str] = []
    mcp: list[str] = []
    other: list[str] = []
    shell_all = False
    for r in rights or []:
        m = _RULE.match(str(r).strip())
        if not m:
            other.append(str(r))
            continue
        verb, arg = m.group(1), (m.group(2) or "")
        if verb in _FILE_VERBS:
            p = arg or "*"
            if p not in files:
                files.append(p)
        elif verb == "Bash":
            if arg in ("", "*"):
                shell_all = True
            else:
                s = arg[:-2] if arg.endswith(":*") else arg
                if s not in shell:
                    shell.append(s)
        elif verb in _WEB_VERBS:
            if verb not in web:
                web.append(verb)
        elif verb.startswith("mcp__"):
            name = verb[len("mcp__"):].rstrip("_*")
            if name and name not in mcp:
                mcp.append(name)
        else:
            other.append(str(r))
    return {
        "files": files,
        "shell": "everything" if shell_all else shell,
        "web": web,
        "mcp": mcp,
        "other": other,
        "count": len(rights or []),
    }


def _avatar_data_uri(project_dir: str, name: str, accent: str) -> str:
    """Inline avatar for the page: the agent's own SVG or a shrunk PNG, else
    an initial on the identity mark. Always a data: URI, never a file path."""
    d = Path(project_dir) if project_dir else None
    if d and (d / "avatar.svg").is_file():
        try:
            raw = (d / "avatar.svg").read_bytes()
            if len(raw) <= _AVATAR_MAX_BYTES:
                return "data:image/svg+xml;base64," + base64.b64encode(raw).decode()
        except OSError:
            pass
    if d and (d / "avatar.png").is_file():
        try:
            raw = (d / "avatar.png").read_bytes()
            try:
                from PIL import Image  # transitive dependency; optional here
                im = Image.open(io.BytesIO(raw)).convert("RGBA")
                im.thumbnail((_AVATAR_PX, _AVATAR_PX))
                buf = io.BytesIO()
                im.save(buf, "PNG", optimize=True)
                raw = buf.getvalue()
            except Exception:
                pass
            if len(raw) <= _AVATAR_MAX_BYTES:
                return "data:image/png;base64," + base64.b64encode(raw).decode()
        except OSError:
            pass
    initial = (name or "?")[:1].upper().replace("&", "&amp;").replace("<", "&lt;")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
           f'<rect width="64" height="64" rx="12" fill="#12121a"/>'
           f'<rect width="3" height="64" fill="{accent}"/>'
           f'<text x="34" y="43" text-anchor="middle" font-family="JetBrains Mono,Menlo,monospace" '
           f'font-weight="700" font-size="30" fill="#ff6b6b">{initial}</text></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


_TIER_COLOR = {"privileged": "#ff4444", "coordinator": "#ffffff", "assistant": "#888888"}


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
        name = a.get("name") or a.get("id")
        rights = a.get("rights", [])
        nodes.append({
            "id": aid,
            "name": name,
            "tier": tier,
            "purpose": a.get("role") or a.get("domain") or cfg.get("description", "") or "",
            "model": a.get("model", ""),
            "tools": tools if isinstance(tools, str) else ", ".join(tools),
            "tool_count": registered_tools if tools == "all" else (len(tools) if isinstance(tools, list) else 0),
            "skills": skills if isinstance(skills, str) else ", ".join(skills),
            "rights": rights,
            "rights_summary": _summarize_rights(rights),
            "reports_to": a.get("reports_to", ""),
            "discord": a.get("discord", ""),
            "schedules": schedules,
            "harness": _harness(cfg, a.get("model", ""), overrides),
            "access": _access(tier, cfg),
            "avatar": _avatar_data_uri(cfg.get("project_dir", ""), name,
                                       _TIER_COLOR.get(tier, "#888888")),
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
:root{--base:#0a0a0f;--mid:#12121a;--deep:#0d1117;--fg:#fff;--fg2:#aaa;--fg3:#888;--line:#333;--accent:#ff4444;--accent2:#ff6b6b;--card:rgba(255,255,255,.03);--cardh:rgba(255,68,68,.08)}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--fg);font:14px/1.6 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:linear-gradient(160deg,var(--base) 0%%,var(--mid) 50%%,var(--deep) 100%%)}
.mono{font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{padding:22px 28px 14px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:10px 28px;align-items:flex-end}
.lbl{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-weight:700;font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--accent2)}
.lbl::before{content:"// ";color:var(--accent)}
header h1{margin:4px 0 2px;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:20px;font-weight:700}
header p{margin:0;color:var(--fg2);font-size:13px}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--fg2);margin-left:auto}
.legend span::before{content:"";display:inline-block;width:10px;height:10px;border-radius:50%%;margin-right:6px;vertical-align:-1px;border:2px solid var(--c)}
#wrap{display:flex;flex-wrap:wrap;border-bottom:1px solid var(--line)}
#graph{flex:1 1 460px;min-height:56vh;position:relative}
svg{width:100%%;height:100%%;display:block}
.edge{stroke:var(--line);stroke-width:2}
.node{cursor:pointer}.node circle.ring{fill:var(--base);stroke-width:3;transition:stroke-width .1s}
.node:hover circle.ring,.node.sel circle.ring{stroke-width:5}
.node text{fill:var(--fg);font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:12px;font-weight:700;text-anchor:middle;pointer-events:none}
.node .sub{fill:var(--fg3);font-size:10px;font-weight:400}
aside{flex:0 0 380px;max-width:100%%;border-left:1px solid var(--line);padding:20px 24px;max-height:80vh;overflow:auto}
.who{display:flex;align-items:center;gap:12px}
.who img{width:48px;height:48px;border-radius:12px;background:var(--mid)}
.who h2{margin:0;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:17px}
.who .t{color:var(--fg2);font-size:12px}
.purpose{color:var(--fg2);margin:10px 0 6px;font-size:13px}
details{border-top:1px solid var(--line);padding:6px 0}
details:last-of-type{border-bottom:1px solid var(--line)}
summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-weight:700;font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--accent2);padding:4px 0}
summary::-webkit-details-marker{display:none}
summary::before{content:"// ";color:var(--accent)}
summary .n{color:var(--fg3);font-weight:400;letter-spacing:0;text-transform:none;font-family:Inter,sans-serif;font-size:12px;margin-left:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:60%%}
summary .n::before{content:none}
details[open] summary .n{display:none}
.pane{padding:4px 0 8px;font-size:13px}
code{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:12px;background:var(--card);border:1px solid var(--line);border-radius:3px;padding:1px 6px;margin:2px 4px 2px 0;display:inline-block;color:var(--fg2);word-break:break-all}
.chip{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:11px;padding:2px 8px;border-radius:3px;border:1px solid var(--line);color:var(--fg2);white-space:nowrap}
.chip.tier{border-color:var(--c);color:var(--c)}
.chip.h{color:var(--fg)}
.hint{color:var(--fg3)}
.kv{display:grid;grid-template-columns:96px 1fr;gap:3px 12px}
.kv dt{color:var(--fg3);font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:11px;letter-spacing:.06em;text-transform:uppercase;padding-top:2px}
.kv dd{margin:0}
.warn{color:var(--accent2)}
#roster{padding:22px 28px 40px}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:10px 0 14px}
.bar input{background:var(--card);border:1px solid var(--line);color:var(--fg);padding:6px 10px;border-radius:3px;font:13px Inter,sans-serif;min-width:220px}
.bar button,.bar label{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:11px;letter-spacing:.06em;text-transform:uppercase;background:none;border:1px solid var(--line);color:var(--fg2);padding:5px 10px;border-radius:3px;cursor:pointer}
.bar button:hover,.bar label:hover{border-color:var(--accent2);color:var(--fg)}
.bar label.on{border-color:var(--accent);color:var(--fg)}
.bar input[type=checkbox]{display:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:14px}
.card{background:var(--card);border-left:3px solid var(--accent);padding:1rem 1.3rem 0.6rem;line-height:1.5}
.card:hover{background:var(--cardh)}
.card.sel{border-left-color:var(--accent2);background:var(--cardh)}
.card.hide{display:none}
.card .who{cursor:pointer}
.card .who img{width:44px;height:44px}
.card .who h4{margin:0;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:15px}
.meta{margin-top:2px;font-family:"JetBrains Mono",ui-monospace,Menlo,monospace;font-size:11.5px;color:var(--fg3);letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta .tier{color:var(--c)}.meta .tier::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%%;background:var(--c);margin-right:6px;vertical-align:1px}
.meta .sep{color:var(--line);margin:0 7px}
.meta b{color:var(--fg2);font-weight:600}
@media(max-width:700px){aside{flex-basis:100%%;border-left:0;border-top:1px solid var(--line);max-height:none}}
</style></head><body>
<header>
  <div><div class="lbl">Agent map</div><h1>%(title)s</h1><p>%(count)d agents · hub <span class="mono">%(hub_name)s</span> · click an agent for detail</p></div>
  <div class="legend"><span style="--c:#ff4444">privileged: full CLI, every tool</span><span style="--c:#ffffff">coordinator: no CLI, every tool, hub</span><span style="--c:#888888">assistant: no CLI, safe tools</span></div>
</header>
<div id="wrap">
  <div id="graph"><svg id="svg"></svg></div>
  <aside id="panel"><p class="hint">Select an agent.</p></aside>
</div>
<div id="roster">
  <div class="lbl">Roster</div>
  <div class="bar">
    <input id="q" type="search" placeholder="filter by name, purpose, model, path…">
    <label id="f-claude"><input type="checkbox" data-h="claude_code">Claude Code</label>
    <label id="f-codex"><input type="checkbox" data-h="codex_cli">Codex CLI</label>
    <label id="f-local"><input type="checkbox" data-h="local">Local</label>
    <button id="open-all">Expand all</button><button id="close-all">Collapse all</button>
  </div>
  <div class="grid" id="grid"></div>
</div>
<script>
const NODES = %(nodes)s, HUB = %(hub)s;
const TIER_C = {privileged:"#ff4444", coordinator:"#ffffff", assistant:"#888888"};
const svg = document.getElementById('svg'), panel = document.getElementById('panel'), grid = document.getElementById('grid');
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function codes(arr){return (arr||[]).length?arr.map(r=>`<code>${esc(r)}</code>`).join(''):'<span class="hint">none</span>';}
function harnessLine(n){const h=n.harness||{};let s=`<b>${esc(h.label||'?')}</b>`;if(h.model)s+=` · <span class="mono">${esc(h.model)}</span>`;return s;}
function harnessPane(n){const h=n.harness||{};const rows=[['Harness',esc(h.label||'?')],['Model',h.model?`<span class="mono">${esc(h.model)}</span>`:'<span class="hint">provider default</span>']];
  if(h.effort)rows.push(['Effort',esc(h.effort)]);if(h.sandbox)rows.push(['Sandbox',esc(h.sandbox)]);
  rows.push(['Source',(h.overridden&&h.overridden.length)?`runtime override on ${esc(h.overridden.join(', '))} (agent_config)`:'agents.yaml']);return kv(rows);}
function accessPane(n){const a=n.access||{};const rows=[['Tier',`<span class="chip tier" style="--c:${TIER_C[a.tier]||'#888'}">${esc(a.tier)}</span> ${esc(a.tier_means||'')}`]];
  rows.push(['Shell',a.denied_builtins&&a.denied_builtins.includes('Bash')?'<span class="warn">Bash denied</span>':(n.rights_summary&&n.rights_summary.shell==='everything'?'everything':(n.rights_summary&&n.rights_summary.shell.length?codes(n.rights_summary.shell):'<span class="hint">no shell rule</span>'))]);
  if(a.denied_builtins&&a.denied_builtins.length)rows.push(['Denied',codes(a.denied_builtins)]);
  if(a.extra_dirs&&a.extra_dirs.length)rows.push(['Also sees',codes(a.extra_dirs)]);
  if(a.privileged_scaffold)rows.push(['Scaffold','privileged: unsandboxed CLI']);return kv(rows);}
function accessLine(n){const a=n.access||{};const bits=[a.tier];if(a.denied_builtins&&a.denied_builtins.includes('Bash'))bits.push('no Bash');else if(n.rights_summary&&n.rights_summary.shell==='everything')bits.push('full shell');if(a.extra_dirs&&a.extra_dirs.length)bits.push(`+${a.extra_dirs.length} dir${a.extra_dirs.length>1?'s':''}`);return bits.join(' · ');}
function toolsPane(n){const rows=[['Tools',n.tools==='all'?`all tools${n.tool_count?` (${n.tool_count} registered)`:''}`:codes(String(n.tools||'').split(', ').filter(Boolean))],['Skills',n.skills==='all'?'all skills':codes(String(n.skills||'').split(', ').filter(Boolean))]];return kv(rows);}
function toolsLine(n){return `${n.tools==='all'?'all tools':n.tools} · ${n.skills==='all'?'all skills':n.skills}`;}
function rightsPane(n){const r=n.rights_summary||{};const rows=[['Files',codes(r.files)],['Shell',r.shell==='everything'?'everything':codes(r.shell)],['Web',(r.web||[]).length?esc(r.web.join(', ')):'<span class="hint">none</span>'],['MCP',codes(r.mcp)]];
  if(r.other&&r.other.length)rows.push(['Other',codes(r.other)]);rows.push(['Raw',`<details><summary style="color:var(--fg3);font-weight:400;letter-spacing:0;text-transform:none">${r.count||0} rules</summary><div class="pane">${codes(n.rights)}</div></details>`]);return kv(rows);}
function rightsLine(n){const r=n.rights_summary||{};const b=[];if(r.files&&r.files.length)b.push(`${r.files.length} path${r.files.length>1?'s':''}`);if(r.shell==='everything')b.push('shell: all');else if(r.shell&&r.shell.length)b.push(`shell: ${r.shell.length}`);if(r.mcp&&r.mcp.length)b.push(`mcp: ${r.mcp.join(', ')}`);return b.join(' · ')||'none';}
function schedPane(n){return (n.schedules||[]).map(s=>`<div style="margin:4px 0"><code>${esc(s.id)}</code> <span class="hint">${esc(s.timing)}</span><br>${esc(s.instruction)}</div>`).join('')||'<span class="hint">none</span>';}
function kv(rows){return `<dl class="kv">${rows.map(([k,v])=>`<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>`;}
function pane(title,line,body,open){return `<details${open?' open':''}><summary>${title}<span class="n">${line}</span></summary><div class="pane">${body}</div></details>`;}
function panes(n,open){return pane('Harness',harnessLine(n),harnessPane(n),open)+pane('Access',accessLine(n),accessPane(n),open)+pane('Tools & skills',toolsLine(n),toolsPane(n),open)+pane('Rights',rightsLine(n),rightsPane(n),open)+pane(`Schedules (${(n.schedules||[]).length})`,(n.schedules||[]).map(s=>s.timing).join(', ')||'none',schedPane(n),open&&(n.schedules||[]).length>0);}
function who(n,tag){const h=(n.harness&&n.harness.label)||'?';const rt=n.reports_to?`reports to <b>${esc(n.reports_to)}</b>`:'hub';
  return `<div class="who"><img src="${n.avatar}" alt=""><div><${tag}>${esc(n.name)}</${tag}><div class="meta"><span class="tier" style="--c:${TIER_C[n.tier]||'#888'}">${esc(n.tier)}</span><span class="sep">·</span>${esc(h)}<span class="sep">·</span>${rt}</div></div></div>`;}
function layout(){
  const g=document.getElementById('graph'), W=g.clientWidth||600, H=g.clientHeight||500;
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`); svg.innerHTML='';
  const cx=W/2, cy=H/2, R=Math.max(100,Math.min(W,H)/2-84);
  const others=NODES.filter(n=>n.id!==HUB), hub=NODES.find(n=>n.id===HUB)||NODES[0];
  const pos={}; if(hub)pos[hub.id]=[cx,cy];
  others.forEach((n,i)=>{const a=-Math.PI/2+2*Math.PI*i/others.length; pos[n.id]=[cx+R*Math.cos(a),cy+R*Math.sin(a)];});
  const NS='http://www.w3.org/2000/svg';
  const defs=document.createElementNS(NS,'defs');svg.appendChild(defs);
  NODES.forEach(n=>{const tgt=(n.reports_to&&pos[n.reports_to])?n.reports_to:(hub&&n.id!==hub.id?hub.id:null);
    if(tgt&&pos[n.id]&&pos[tgt]){const l=document.createElementNS(NS,'line');l.setAttribute('class','edge');
    l.setAttribute('x1',pos[n.id][0]);l.setAttribute('y1',pos[n.id][1]);l.setAttribute('x2',pos[tgt][0]);l.setAttribute('y2',pos[tgt][1]);svg.appendChild(l);}});
  NODES.forEach((n,i)=>{const[x,y]=pos[n.id]||[cx,cy]; const isHub=hub&&n.id===hub.id; const r=isHub?36:28;
    const cp=document.createElementNS(NS,'clipPath');cp.id='clip'+i;const cc=document.createElementNS(NS,'circle');cc.setAttribute('r',r-3);cp.appendChild(cc);defs.appendChild(cp);
    const grp=document.createElementNS(NS,'g');grp.setAttribute('class','node');grp.dataset.id=n.id;grp.setAttribute('transform',`translate(${x},${y})`);
    const c=document.createElementNS(NS,'circle');c.setAttribute('class','ring');c.setAttribute('r',r);c.setAttribute('stroke',TIER_C[n.tier]||'#888');grp.appendChild(c);
    if(n.avatar){const im=document.createElementNS(NS,'image');im.setAttribute('href',n.avatar);im.setAttribute('x',-(r-3));im.setAttribute('y',-(r-3));im.setAttribute('width',2*(r-3));im.setAttribute('height',2*(r-3));im.setAttribute('clip-path',`url(#clip${i})`);im.setAttribute('preserveAspectRatio','xMidYMid slice');grp.appendChild(im);}
    const t=document.createElementNS(NS,'text');t.setAttribute('y',r+16);t.textContent=n.name;grp.appendChild(t);
    const tt=document.createElementNS(NS,'text');tt.setAttribute('class','sub');tt.setAttribute('y',r+29);tt.textContent=(n.harness&&n.harness.label)||n.tier;grp.appendChild(tt);
    if(n.schedules&&n.schedules.length){const b=document.createElementNS(NS,'text');b.setAttribute('x',r-2);b.setAttribute('y',-r+6);b.setAttribute('text-anchor','end');b.setAttribute('font-size','13');b.textContent='⏰'+n.schedules.length;grp.appendChild(b);}
    grp.addEventListener('click',()=>select(n));svg.appendChild(grp);});
}
function roster(){grid.innerHTML=NODES.map(n=>`<div class="card" data-id="${esc(n.id)}" data-h="${esc((n.harness&&n.harness.provider)||'')}">${who(n,'h4')}<div class="purpose">${esc(n.purpose)||'<span class=hint>no purpose recorded</span>'}</div>${panes(n,false)}</div>`).join('');
  grid.querySelectorAll('.card .who').forEach(el=>el.addEventListener('click',()=>select(NODES.find(n=>n.id===el.parentElement.dataset.id))));}
function select(n){
  document.querySelectorAll('.node,.card').forEach(el=>el.classList.toggle('sel',el.dataset.id===n.id));
  panel.innerHTML=who(n,'h2')+`<div class="purpose">${esc(n.purpose)||'<span class=hint>no purpose recorded</span>'}</div>`+panes(n,true)+(n.discord?`<p class="hint" style="font-size:12px">Discord ID <code>${esc(n.discord)}</code> · id <code>${esc(n.id)}</code></p>`:'');
  const card=grid.querySelector(`.card[data-id="${CSS.escape(n.id)}"]`);if(card&&card.classList.contains('hide')){card.classList.remove('hide');}
}
function applyFilter(){const q=(document.getElementById('q').value||'').toLowerCase();const hs=[...document.querySelectorAll('.bar input[type=checkbox]:checked')].map(i=>i.dataset.h);
  document.querySelectorAll('.bar label').forEach(l=>l.classList.toggle('on',l.querySelector('input').checked));
  grid.querySelectorAll('.card').forEach(el=>{const n=NODES.find(x=>x.id===el.dataset.id);const hay=JSON.stringify([n.name,n.id,n.purpose,n.harness,n.access,n.rights_summary,n.reports_to]).toLowerCase();
    const ok=(!q||hay.includes(q))&&(!hs.length||hs.includes(el.dataset.h));el.classList.toggle('hide',!ok);});}
document.getElementById('q').addEventListener('input',applyFilter);
document.querySelectorAll('.bar input[type=checkbox]').forEach(i=>i.addEventListener('change',applyFilter));
document.getElementById('open-all').addEventListener('click',()=>grid.querySelectorAll('details').forEach(d=>d.open=true));
document.getElementById('close-all').addEventListener('click',()=>grid.querySelectorAll('details').forEach(d=>d.open=false));
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
        "Generate an interactive HTML map of all agents — avatars in a graph "
        "around the hub, plus a roster card per agent with purpose, harness "
        "(provider and model actually in use), reports-to, access, tools and "
        "skills, rights summarised by what they grant, and schedules, in "
        "accordion panes with search and filters. Returns a file path; deliver "
        "it to the user with send_discord_file."
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
