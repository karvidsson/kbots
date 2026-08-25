# kbots — Architecture & Operations Reference

> Last updated: 2026-08-10

## System Overview

A single async Python process is the whole of kbots: it bridges Discord bots to Claude Code CLI sessions and layers on MCP-exposed tools, persistent memory, an encrypted credential vault, three-layer access control, and a complete security middleware stack.

One checkout can power several instances, each started with its own `--profile` config — for example a locked-down production service alongside an unrestricted dev/ops one. All instances read and write the same SQLite databases (safe to share thanks to WAL mode plus busy_timeout) and open the same Fernet vault.

---

## Architecture

```
Discord (one or more bot accounts)
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│              kbots Process                             │
│                                                          │
│  Discord Connector (multi-bot, typing, slash commands,   │
│    loop detection, dedup, reply fallback)                 │
│       │                                                  │
│       ▼                                                  │
│  Router (channel/guild/bot/category → agent mapping)     │
│       │                                                  │
│       ▼                                                  │
│  Access Control (three-layer)                            │
│   ├── Layer 1: Can they talk? (sender tier → agent tier) │
│   ├── Layer 2: What tools? (--disallowedTools per sender)│
│   └── Layer 3: Agent ceiling (static config per agent)   │
│       │                                                  │
│       ▼                                                  │
│  Agent Manager                                           │
│   ├── Context injection (channel, user, attachments)     │
│   ├── Auto-recall (memory search before each LLM call)   │
│   ├── Session management (SQLite, --resume)              │
│   └── Auto-summarize (long conversations)                │
│       │                                                  │
│       ▼                                                  │
│  Claude Code CLI (--print --output-format json)          │
│   ├── Reads agent identity (AGENTS.md) from project_dir           │
│   ├── Built-in tools: Read, Write, Bash, Glob, Grep,    │
│   │   WebSearch, WebFetch (blockable via disallow_builtins)│
│   └── MCP tools: custom tools via kbots-tools server      │
│       │                                                  │
│       ▼                                                  │
│  MCP Server (FastMCP SDK, stdio transport)               │
│   ├── Rate limit check                                   │
│   ├── HITL gate (Discord reaction approval)              │
│   ├── Tool execution (@tool Python functions)            │
│   ├── Audit log (JSONL)                                  │
│   └── Vault access (Fernet encrypted credentials)        │
│                                                          │
│  Storage: SQLite (sessions, messages, tool logs)         │
│  Hot reload: file watcher on skills/ and src/tools/      │
└─────────────────────────────────────────────────────────┘
         │
         ▼
   External APIs (whatever you connect)
   • Google (OAuth2)
   • Discord API
   • Tavily, Groq, Gemini
   • Trello, Notion, Cloudflare
   • Your own integrations
```

## Three-Layer Architecture

The directory structure is split into layers so that the engine, any domain extensions, and each deployment's own config live apart from one another. The payoff is a Core repo that stays easy to update and a public codebase that deployment data can never bleed into.

```
Layer 1 (Core):    $KBOTS_HOME           ← Engine code, generic tools, scripts
Layer 2 (Modules): $KBOTS_MODULES           ← Domain-specific tools/skills (per-module subdirs)
Layer 3 (Overlay): $KBOTS_OVERLAY        ← This deployment's config, agents, data
```

| Layer | Contains | Git workflow | Env var |
|-------|----------|-------------|---------|
| **Core** | `src/`, generic tools, skills, scripts, systemd templates | Branch + PR + cross-review | — |
| **Layer 2** | Domain tools/skills in per-module dirs (e.g. `crm/`, `analytics/`) | Self-merge OK | `KBOTS_MODULES` |
| **Overlay** | `config/`, `agents/`, `systemd/`, `data/`, agent identities | Direct push | `KBOTS_OVERLAY` |

**What goes where:**
- Core → engine code and generic tools
- Layer 2 → domain tools (custom integrations, business logic)
- Overlay → `config.yaml`, `agents.yaml`, `team.json`, agent AGENTS.md files, service units
- **Never in Core or Layer 2** → personal names, Discord IDs, API keys, company data

**Setup:** During Step 2, `setup.py` builds the overlay directory on its own and drops every piece of generated config into it. Systemd service units get the `KBOTS_OVERLAY` env var baked in, which is how the running process locates its config.

**Discovery:** At startup the engine walks the Layer 2 module directories; any subdirectory containing a `tools/` or `skills/` folder is picked up automatically. Which modules are active is chosen in `setup.py` Step 4, which also assembles the `KBOTS_MODULES` path.

### Remote Neutralisation

For anyone who isn't a maintainer, running `setup.py` after cloning Core rewires git: the `origin` remote becomes `upstream` and its push URL is disabled. With no pushable remote left, neither an agent nor a user can accidentally publish deployment data to the public Core repo.

```
origin  → upstream (fetch: github.com/karvidsson/kbots, push: blocked)
origin  → (unset — nothing to push to by default)
```

Pulling updates keeps working via `git pull upstream main`. A maintainer who needs to push again can undo the neutralisation:

```bash
git remote rename upstream origin
git remote set-url --push origin <url>
```

### Deployment Patterns

There are two ways to lay out a kbots deployment, and both keep Core untouched. What separates them is where domain-specific tools go: into a dedicated Layer 2 repo, or straight into the overlay next to your config.

#### Two-Layer: Core + Overlay

This is the minimal setup: the engine lives in one repo, and a second repo (or plain directory) holds everything unique to the install — config, agents, custom tools and skills, systemd units, and data.

```
Layer 1 (Core):    /opt/kbots              ← git clone of karvidsson/kbots (or your fork)
Layer 3 (Overlay): /opt/kbots-myproject    ← your private repo with config + agents + tools
```

Choose this pattern when:
- You run a **single deployment** — one server, one roster of agents
- Your custom tools serve only this install and nothing else will reuse them
- You'd rather keep the setup as lean as possible

An overlay directory looks like this:

```
my-overlay/
├── config/
│   ├── config.yaml          # Main config (connectors, security, defaults)
│   ├── agents.yaml          # Agent definitions and routing
│   ├── team.json            # Team roster (people + bots)
│   ├── secrets.enc          # Fernet vault (gitignored)
│   └── secrets.salt         # Vault salt (gitignored)
├── agents/
│   ├── main/                # Agent project directory
│   │   ├── AGENTS.md        # Agent identity (CLAUDE.md stub imports it)
│   │   ├── codex/           # Per-agent knowledge base (own _index.md)
│   │   ├── .mcp.json        # MCP server config (generated by setup.py)
│   │   └── .claude/
│   │       └── settings.json
│   └── ops/                 # Optional second agent
├── tools/                   # Custom tools (auto-discovered)
│   └── my_integration.py
├── skills/                  # Custom skills (auto-discovered)
│   └── my_workflow.yaml
├── systemd/                 # Generated service/timer units
├── codex/                   # Shared business knowledge base
├── scripts/                 # Deployment-specific scripts
├── data/                    # Runtime data (gitignored)
└── tmp/                     # Agent-generated files (gitignored)
    ├── media/
    ├── docs/
    └── scratch/
```

You don't build this tree by hand — `setup.py` generates it. Point `KBOTS_OVERLAY` at it and setup is complete; `KBOTS_MODULES` stays unset.

#### Three-Layer: Core + Modules + Overlay

Meant for organisations operating **multiple deployments** that want common domain tools while each install keeps its own config, agents, and data.

```
Layer 1 (Core):    /opt/kbots              ← Engine (shared across all deployments)
Layer 2 (Modules): /opt/kbots-modules      ← Domain tools/skills (shared across deployments)
Layer 3 (Overlay): /opt/kbots-deploy-a     ← Deployment A config + agents
                   /opt/kbots-deploy-b     ← Deployment B config + agents (different server)
```

Reach for this pattern when:
- Several **deployments consume one shared set** of custom tools
- Each install carries its own agent configuration, team roster, or security settings
- Shared tools should be maintainable centrally, with no per-deployment edits

Layer 2 is expressed as a colon-separated list of module directories, where every module contributes a `tools/` and/or `skills/` subdirectory of its own:

```
kbots-modules/
├── crm/
│   ├── tools/
│   │   └── salesforce.py
│   └── skills/
│       └── pipeline_report.yaml
├── analytics/
│   └── tools/
│       └── bigquery.py
└── ops/
    ├── tools/
    │   └── system_audit.py
    └── skills/
        └── system_audit.yaml
```

Module selection happens per deployment in `setup.py` Step 4 — one install could enable `crm` + `analytics` while another runs with just `ops`. Whatever gets chosen ends up encoded in the `KBOTS_MODULES` env var:

```bash
KBOTS_MODULES=/opt/kbots-modules/crm:/opt/kbots-modules/analytics
```

#### Discovery Order

Discovery of tools and skills proceeds layer by layer, and a name defined in more than one layer resolves to the later one — the overlay beats modules, and modules beat core:

```
Core tools/skills → Layer 2 modules → Overlay tools/skills → Agent-specific skills
```

`resolve_config_file()` applies the matching precedence when looking up config files: overlay → modules → core.

### Keeping Core Clean

Core is the engine everyone shares — public, redistributable, and the upstream that all deployments track. Anything deployment-specific that slips in gets propagated to every install that pulls.

**Rules:**

1. **No personal data.** Core must stay free of names, Discord IDs, email addresses, company names, and API keys — no exceptions, whether the location is a config file, a code comment, or a commit message.

2. **No deployment config.** Live `config.yaml`, `agents.yaml`, and `team.json` files belong to the overlay; the only versions shipped in Core are the `.example` templates.

3. **No domain tools.** Business-specific tooling — a CRM integration, a custom ETL job, a proprietary API client — lives in Layer 2 or the overlay. What Core ships are general-purpose utilities that any deployment can use.

4. **No agent identities.** An AGENTS.md that defines a named agent — its personality, permissions, and standing instructions — is overlay material; Core carries nothing beyond example templates.

5. **No hardcoded paths.** Every path inside Core goes through `PROJECT_ROOT` (Python), `$KBOTS_HOME` (shell), or an env var. Where the systemd templates say `/opt/kbots`, that is a placeholder token that `setup.py` swaps for the real install path during setup.

6. **Branch workflow.** A pre-commit hook refuses commits made directly on `main`; work lands via feature branches and reviewed PRs instead, shielding every downstream deployment from untested changes.

**Why this matters:** A clean Core is what makes `git pull` painless — updates arrive without a single merge conflict, and your agents, config, and tools stay exactly as they were. Were deployment data mixed into Core, each pull would turn into conflict resolution, and sooner or later you'd give up on pulling at all — falling behind on security fixes and features.

Most of this discipline is enforced by the `setup.py` wizard itself: non-maintainers get their git remote renamed so pushes are blocked, generated config is written only to the overlay, and `.gitignore` rules cover runtime data. Polluting Core by accident takes real effort under this design.

---

## Key Architectural Decisions

1. **Claude Code CLI is a black box** — tool execution happens in the MCP subprocess rather than the main process, which leaves the `_dispatch_tools()` loop in agent_manager as dead code whenever the Claude Code provider is in use.

2. **MCP server IS the middleware layer** — the MCP tool handlers, not agent_manager, are where rate limiting, HITL, and auditing take place.

3. **All paths must be absolute** — the `cwd` field in `.mcp.json` is ignored by Claude Code, so MCP tools anchor every file reference with `Path(__file__).resolve().parent.parent`.

4. **`KBOTS_PROFILE` env var** — the single switch selecting test or production config at every link of the chain (main.py → Claude Code → MCP server).

5. **HITL always uses the primary bot token** — no matter which agent is running or which profile is loaded.

6. **One MCP server per Claude Code session** — every agent gets its own MCP server subprocess, and each of those servers loads the vault independently.

---

## Directory Layout

```
kbots/
├── src/
│   ├── core/
│   │   ├── agent_manager.py   — sessions, context injection, LLM dispatch
│   │   ├── access_control.py  — three-layer permission system (sender tier × agent tier)
│   │   ├── registry.py        — auto-discovery of all modules
│   │   ├── router.py          — message routing: connector/channel/category → agent
│   │   ├── tools.py           — @tool decorator, schema from type hints
│   │   ├── skills.py          — YAML skill loader
│   │   ├── storage.py         — SQLite (sessions, messages, tool logs)
│   │   ├── hitl.py            — HITL approval gates (connector-side, for main process)
│   │   ├── rate_limiter.py    — per-tool per-agent sliding window
│   │   ├── audit.py           — append-only JSONL audit log
│   │   ├── content_safety.py  — behavioral monitoring
│   │   ├── digest.py          — hot-reload file watcher
│   │   └── base.py            — interfaces, dataclasses
│   ├── connectors/
│   │   └── discord.py         — multi-bot, typing, slash commands, loop detection, dedup
│   ├── llm/
│   │   └── claude_code.py     — Claude Code CLI (Pro/Max sub or API billing)
│   ├── tools/                 — @tool decorated functions (auto-discovered)
│   │   ├── memory.py          — store, search, semantic_search, forget
│   │   ├── team.py            — list, get, add, update, remove + user context
│   │   ├── web_search.py      — Tavily search
│   │   ├── audio.py           — transcribe_audio (Groq Whisper)
│   │   ├── discord_tools.py   — read_channel, read_message, search, send_file
│   │   ├── video.py           — video_frames, video_clip
│   │   ├── tmux.py            — list, send, read, new
│   │   ├── compress.py        — compress_context, decompress_context
│   │   ├── ingest.py          — create_skill, read_url, browse_url, install_mcp, list_capabilities
│   │   └── builtin.py         — send_message, ask_agent, send_to_agent
│   ├── lib/
│   │   └── compressor.py      — rule-based context compression (no LLM calls)
│   ├── vault/
│   │   └── fernet.py          — Fernet encrypted vault
│   ├── auth/
│   │   └── oauth2.py          — OAuth2 refresh (Google, etc.)
│   ├── mcp_server.py          — FastMCP server with middleware chain
│   └── main.py                — entry point, --profile support
├── agents/
│   └── rescue/                — example agent template (AGENTS.md.example)
├── config/
│   ├── config.yaml.example    — example production config
│   ├── agents.yaml.example    — example agent definitions
│   ├── team.json.example      — example team roster
│   ├── kbots.service        — systemd unit template
│   ├── kbots-rescue.service    — systemd unit template (unsandboxed)
│   └── timers/                — systemd timer+service files for scheduled tasks
├── scripts/
│   ├── test-tools.py          — e2e tests across tool categories
│   ├── health-audit.sh        — full system audit (timer + on-demand via /system-audit)
│   ├── monitor-integrity.sh   — file integrity SHA256
│   ├── regen-memory-context.sh — memory stats snapshot
│   ├── memory-decay.sh        — memory decay/archive/purge
│   ├── compress-context.sh    — batch context file compression
│   ├── install-timers.sh      — install all systemd timers (legacy)
│   ├── install-systemd.sh     — symlink units, daemon-reload, enable timers
│   ├── google-reauth.py       — Google OAuth2 re-authentication helper
│   └── lib-alert.sh           — shared Discord alert helper
├── skills/                    — YAML skill definitions (auto-discovered)
├── extras/                    — opt-in integrations (google, trello, stocks…) — NOT auto-discovered; install via cp into the overlay (see extras/README.md)
├── data/                      — SQLite DBs, audit logs (gitignored)
├── vault-manage.py            — interactive vault secret manager
├── setup.py                   — interactive setup wizard (single entry point)
└── setup.sh                   — deprecated stub (redirects to setup.py)
```

---

## Agents

Agents are declared in `config/agents.yaml`, and each one may carry its own bot account, its own tool access list, and its own channel/category routing.

Some example agent archetypes:

| Type | Built-in Tools | MCP Tools | Use Case |
|------|---------------|-----------|----------|
| **Coordinator** | Blocked (Edit, Write, Bash, MultiEdit) | All | Runs business operations using nothing but MCP tools |
| **Privileged** | Full access | All | Dev/ops work: administration, code edits, deploys |
| **Assistant** | Blocked | Restricted whitelist | Narrow role with access trimmed to what the job requires |

### Access Control (Three-Layer)

Implemented in `src/core/access_control.py`, with its data drawn from `config/team.json`:

| Layer | What it controls | Where it lives |
|-------|-----------------|----------------|
| **1. Can they talk?** | Sender tier × agent tier decides whether the message is handled or ignored | `access_control.py` |
| **2. What tools?** | Per-message `--disallowedTools` derived from the sender's tier | `access_control.py` → `agent_manager.py` |
| **3. Agent ceiling** | Fixed per-agent config (tools list, disallow_builtins) | `agents.yaml` |

**People tiers:** owner (everything) → admin (safe tools plus HITL) → staff (safe tools, assistant agents only) → unknown (no access)

**Agent tiers:** privileged (CLI and every MCP tool) → coordinator (all MCP, no CLI) → assistant (safe MCP subset, no CLI)

### Agent Config

Every agent points at a `project_dir` holding three things: AGENTS.md for identity and tool documentation (plus a CLAUDE.md stub importing it for Claude Code), `.mcp.json` for the MCP server config, and `.claude/settings.json` for permissions, env, and allowed tools.

```yaml
# config/agents.yaml — example
agents:
  primary:
    display_name: "MyAgent"
    project_dir: ./agents/primary
    tools: all                    # MCP tools ("all" or explicit list)
    disallow_builtins:            # Block Claude Code built-in tools
      - Edit
      - Write
      - Bash
      - MultiEdit
    routing:
      discord:
        account: default
        channels: []              # All channels (wildcard)
```

### Routing

When a message arrives, routing decides which agent picks it up. Every connector defines its own namespace of routing keys (`routing.discord`, `routing.http`, and so on).

**Discord routing — scope vs filters:**

Two mechanisms combine here: **scope** picks the channels an agent listens in, and **filters** layer extra constraints on top of that.

*Scope* — evaluated by priority, stopping at the first match:

| Priority | Config Key | Effect |
|----------|-----------|--------|
| 1 | `channels: [id, ...]` | Matches a channel ID exactly; wins over everything else |
| 2 | `categories: [id, ...]` | Every channel under a given Discord category |
| 3 | `channels: []` (empty) | Wildcard: listen everywhere |
| 4 | DM fallback (implicit) | DMs go to whichever agents are bound to that bot |

*Filters* — checked regardless of how scope matched:

| Filter | Config Key | Effect |
|--------|-----------|--------|
| **Bot account** | `account` | Routing namespace; every bot routes on its own. Required. |
| **Guild/Server** | `guilds: [id, ...]` | Limits matching to the listed servers; an empty list means no limit. |
| **Mentions** | `mentions: true/false` | If enabled, the agent answers only DMs and messages that @mention it. |
| **Users** | `users: [id, ...]` | Only respond to these sender user IDs; an empty list means everyone the team tiers allow. When set, messages carrying no sender ID (system posts) are dropped too. |

Overlapping routing rules are allowed: when several agents match one message, every one of them gets a copy.

**Examples:**

```yaml
# Agent responds to all channels on bot "main", only when @mentioned
routing:
  discord:
    account: main
    channels: []
    mentions: true

# Agent scoped to a specific Discord category
routing:
  discord:
    account: assistant
    categories: ["1234567890"]
    mentions: false

# Agent locked to one channel in one server
routing:
  discord:
    account: ops
    channels: ["9876543210"]
    guilds: ["1111222233"]
    mentions: true
```

**LLM defaults** may carry an `mcp_config` entry, which attaches external MCP servers to every agent at once:

```yaml
defaults:
  llm:
    provider: claude_code
    model: sonnet
    mcp_config: /path/to/mcp.yaml   # Optional: external MCP servers
```

---

## Tools

A tool is nothing more than an async Python function in `src/tools/` carrying the `@tool` decorator. Discovery happens automatically at startup: adding a file is all it takes to make it available.

Tool packs that ship with the engine:

| Category | Tools |
|----------|-------|
| **Memory** | store, search, semantic_search, forget |
| **Team** | list, get, add, update, remove |
| **Google Workspace** | Gmail (search, read, send), Calendar (list, create), Drive (search, list, download, create, upload, delete, share) |
| **Trello** | boards, lists, cards, create_card, activity |
| **Media** | gemini_analyze_image, gemini_analyze_video, gemini_generate_image, transcribe_audio, video_frames, video_clip |
| **Discord** | read_channel_history, read_message, search_channel_history, send_discord_file |
| **Cloudflare** | zones, dns_list, dns_update |
| **Notion** | search, read, create |
| **Web** | web_search (Tavily) |
| **Compression** | compress_context, decompress_context |
| **System** | create_skill, read_url, browse_url, browser, install_mcp, list_capabilities |
| **Tmux** | list, send, read, new |
| **Android** | android_device (screenshot, tap, swipe, type, key, launch, open_url, install — emulator or real phone via ADB, see [docs/ANDROID.md](docs/ANDROID.md)) |
| **Inter-agent** | send_message, ask_agent, send_to_agent |
| **Design** | render_diagram (Mermaid, offline via vendored mermaid.js), render_svg, render_html, html_to_pdf, create_slides |
| **Process mapping** | process_model_save, process_model_load, process_model_gaps, process_render, process_publish — see below |

*Tools flagged for HITL in config pause until a human approves them with a Discord reaction.*

### Process Mapping

Business processes and Wardley maps are captured as a **structured model first, diagram second**. The agent fills a JSON model (`kind: process` — actors, steps, decisions, handoffs, systems, metrics, exceptions; or `kind: wardley` — anchors, components with visibility/evolution and a stage rationale, links, inertia, movement) and the engine does the deterministic work:

- `process_model_save` validates (dangling edges, unlabeled decisions, coordinates outside 0..1, dependencies pointing the wrong way …), **patch-merges** into `<project_dir>/processes/<slug>/model.json` so an interview can add a little each turn, and returns the **ranked next questions** — picked from a method question bank (SIPOC, RACI/swimlanes, value stream mapping, BPMN discovery, Lean wastes, 5 Whys, TOC, service blueprint, event storming, Wardley's canvas and evolution cheat sheet — `src/lib/process_questions.py`) according to which model fields are empty, hedged ("usually", "?") or contradictory. That is how the agent "asks the right questions": never a generic checklist, always the gap.
- `process_model_gaps` re-ranks on demand, optionally through a lens (`sipoc`, `raci`, `vsm`, `bpmn`, `wastes`, `wardley`, `blueprint`, `events`) — the coach mode for a live workshop.
- `process_render` emits diagram text (`src/lib/process_model.py`) and renders it locally: Mermaid `flowchart` / `swimlane-beta` (lane per actor, with a flowchart-with-subgraphs fallback) / `sequenceDiagram` (handoffs) / `journey` through `render_diagram`, and Wardley maps through a self-contained SVG emitter (`src/lib/wardley_svg.py`, no website or CDN involved) plus an `.owm` text twin. Results go to Discord with `send_discord_file`.
- `process_publish` promotes a finished process into the **codex** (`<overlay>/codex/processes/<slug>.md` + PNG, registered in `_index.md`) so every agent sees it in its startup `<codex-index>` and can use it as business knowledge. It is a deliberate, user-confirmed step.

Slash commands, all under the `/process-` prefix: `/process-map` (notes, image/whiteboard photo, interview, update, publish), `/process-wardley`, `/process-questions` (`skills/process_*.yaml`). Images reach the model through `download_file` + the CLI's `Read` (vision); the skills instruct inventory-first extraction and put every unreadable label or unverifiable arrow into `open_questions` rather than guessing.

To wire in your own integration, place a Python file with a `@tool` decorated function in `src/tools/`.

### Tool Development Guide

1. **Create the file** — `src/tools/my_tool.py`, or `<layer2-module>/tools/my_tool.py` when it's a domain tool
2. **Decorate with `@tool`** — supply a name and description, plus `hitl=True` if the tool should be gated
3. **Type-hint parameters** — annotations (str, int, bool, Optional, list) are all the schema generator needs
4. **Accept `ctx: ToolContext`** — your handle to `ctx.vault` (secrets), `ctx.memory` (memory backend), `ctx.project_dir`, `ctx.agent_id` / `ctx.channel_id` (see `src/core/base.py`)
5. **Return a string** — whatever comes back is what the LLM sees as the result

```python
from src.core.base import ToolContext
from src.core.tools import tool

@tool(name="my_tool", description="Does something useful")
async def my_tool(ctx: ToolContext, query: str, limit: int = 10) -> str:
    api_key = ctx.vault.get("my-api-key")
    # ... do work ...
    return f"Found {limit} results for {query}"
```

On the next startup the tool is found automatically — nothing to import, no registry entry to touch — and every agent can reach it through MCP right away.

**Testing:** `uv run python scripts/test-tools.py --tool my_tool`

**HITL gating:** When a tool must always ask for human approval no matter how a deployment is configured, set `hitl=True` on the decorator: `@tool(name="dangerous_tool", description="...", hitl=True)`

### MCP Configuration

Claude Code CLI spawns a dedicated MCP server subprocess (`src/mcp_server.py`) for each agent. Two files govern how those servers are configured:

**Per-agent `.mcp.json`** — sits in the agent's project directory and wires up the kbots tool server:

```json
{
  "mcpServers": {
    "kbots-tools": {
      "command": "<KBOTS_HOME>/.venv/bin/python3",
      "args": ["-m", "src.mcp_server"],
      "cwd": "<KBOTS_HOME>",
      "env": {
        "KBOTS_PROFILE": "${KBOTS_PROFILE:-}",
        "KBOTS_PROJECT_DIR": "<KBOTS_OVERLAY>/agents/main",
        "KBOTS_OVERLAY": "<KBOTS_OVERLAY>"
      }
    }
  }
}
```

> **Note:** You never author this file yourself — `setup.py` writes `.mcp.json` with real absolute paths filled in at setup time. `<KBOTS_HOME>` and `<KBOTS_OVERLAY>` above simply mark where those substitutions land.

**Required env vars:** The MCP server derives the agent ID from `KBOTS_PROJECT_DIR`, and tools that consult overlay config (the health audit, among others) rely on `KBOTS_OVERLAY`. Both are filled in automatically by `setup.py`.

**External MCP servers** — to bring in further MCP servers (third-party tools, other systems), declare them in `config/mcp.yaml`:

```yaml
servers:
  kbots-tools:
    transport: stdio
    command: .venv/bin/python3     # resolved to absolute path by setup.py
    args: ["-m", "src.mcp_server"]

  # Example: connect an external MCP server
  my-server:
    transport: stdio
    command: npx
    args: ["-y", "@my-org/mcp-server"]
```

Setting `mcp_config` under `defaults.llm` exposes the external servers to every agent. From the LLM's perspective their tools are indistinguishable from built-in ones — they surface as native tools.

### Inter-Agent Communication

Agents communicate through `ask_agent` and `send_to_agent`. Both work from any LLM backend, including the Claude Code CLI: tools run inside the MCP server (a subprocess with no handle on the AgentManager), so the calls travel over the **loopback internal API** — an HTTP server in the main process, bound to 127.0.0.1, authenticated with a per-boot bearer token passed via env. A depth guard (`KBOTS_INTER_AGENT_DEPTH`, max 3) is enforced on both sides to prevent A→B→A loops.

**`ask_agent(agent_name, question)` — synchronous Q&A.** Runs the target's turn in a private side-session (`internal:<from>:<to>`) and returns the answer to the caller. The exchange is not posted anywhere; the caller is responsible for surfacing whatever matters. Concurrent asks to the same target serialize on a per-session lock (shared CLI session — concurrent `--resume` collides).

**`send_to_agent(agent_name, message)` — fire-and-forget, delivered visibly.** The message is routed into the target's **home channel**: the incoming message is posted there (`📨 **sender → target:** …`), the target's turn runs in its normal channel session — full conversation context, same queueing/locking as a user message — and the reply is posted to that channel. Nothing is returned to the sender; the tool result says so explicitly. The target's turn gets an `<inter-agent-message>` context block naming the sender, with a `NO_REPLY` abstain path so pure acknowledgements don't spam the channel.

The home channel resolves in order: an explicit `home_channel` in the agent's routing block → the first entry of a concrete `channels` list → the agent's most recently active real conversation channel (for wildcard-routed agents). If nothing resolves (e.g. a connector-less agent), the send falls back to the invisible internal side-session — the pre-visible-delivery behavior — and the tool result warns that the outcome will not surface anywhere.

Why visible delivery: a hidden side-session turn whose response nobody reads is how sends get "lost" — the target's channel self has no trace of the exchange (different CLI session), the owner never sees the outcome, and the sender's "dispatched" reads as delivered-and-seen. Routing through the home channel puts the request, the context, and the reply in one place.

Bot-to-bot @mentions via `send_message` do **not** reliably invoke the target agent and must not be used as a messaging bus — use the tools above.

---

## Services

### Dual-Instance Pattern

To split privileges, kbots can run as a pair of instances off one codebase:

| Instance | Service | Profile | Sandbox | Purpose |
|----------|---------|---------|---------|---------|
| **Main** | `kbots.service` | default | Sandboxed — `ProtectSystem=strict`, `NoNewPrivileges=true`, capabilities dropped | The agents users talk to: business ops, assistants |
| **Ops** | `kbots-rescue.service` | `rescue` | No sandbox — full filesystem plus sudo | One privileged agent handling dev/ops, deploys, and debugging |

The two are the same program (`uv run python -m src.main`) told apart only by `--profile`. SQLite databases (WAL mode) and the Fernet vault are common to both, while lock files are per-instance.

On the main instance, `disallow_builtins` shuts off the built-in tools (Edit, Write, Bash), leaving agents with MCP tools alone. The ops instance opens everything up — and compensates by letting access control confine its use to the owner.

The ops-instance step of `setup.py` optionally provisions the ops instance, producing its own `agents.rescue.yaml` and a bot account reserved for it. The matching unit (`config/kbots-rescue.service`) is installed manually — the wizard prints the exact commands.

### Systemd

```bash
# Main service
systemctl start kbots.service
systemctl status kbots.service
journalctl -u kbots -f

# Ops instance
systemctl start kbots-rescue.service
```

### Sandboxing (Main Instance)

Systemd sandboxing is enabled on the main service unit:

- `ProtectSystem=strict` — everything on disk is read-only apart from the listed `ReadWritePaths`
- `NoNewPrivileges=true` — escalating privileges is impossible
- `CapabilityBoundingSet=` — the capability set is emptied entirely
- `PrivateDevices=true` — physical devices are hidden from the process
- `RestrictNamespaces=true` — creating namespaces is forbidden
- `SystemCallFilter=~@mount @reboot @swap @debug @obsolete` — risky syscall groups are denied
- `ReadWritePaths` — write access covers only the overlay dirs, data, tmp, and the Claude Code cache

None of these restrictions apply to the ops instance — by design.

### File Ownership

Everything under the install directory belongs to `kbots:kbots`, the service user. A root-owned file in the tree is a time bomb: the service can read it but never write it, and a single root-owned file inside `.venv/` is enough to make `uv sync` / `uv run` fail.

If you edit as root — say from a Claude Code session running with root privileges — restore ownership with chown after each save:

```bash
# Check for misowned files
find "$KBOTS_HOME" -not -user kbots 2>/dev/null
# Fix
sudo chown -R kbots:kbots <paths>
```

The service user's **`~/.claude.json` and `~/.claude/`** are part of the same rule and matter even more: a root-owned `~/.claude.json` breaks headless workspace trust and silently denies **every tool for every agent**. Ownership problems on these paths are caught at boot by the preflight and at runtime by [Permission Watch](#permission-watch); the full failure catalog, per-platform setup (Linux / macOS / Windows-WSL2), and prevention hooks are in [docs/PERMISSIONS.md](docs/PERMISSIONS.md).

---

## Security

| Layer | Implementation |
|-------|---------------|
| **Access control** | Sender tier × agent tier evaluated in three layers, with per-message tool filtering and a static agent ceiling |
| **Credentials** | Fernet vault (`config/secrets.enc`), per-instance random salt, PBKDF2 key derivation |
| **HITL** | Gated tool list set in config; approval via Discord reaction; timeouts fail closed by default |
| **Content safety** | Sanitize (log-only), then injection scoring, then behavioral monitoring — a three-stage pipeline |
| **Security alerts** | Content-safety hits and behavioral anomalies alert a configured Discord channel in real time |
| **Permission watch** | Runtime rights-failure detection (broken workspace trust, tool denials, wrong ownership) escalated to a main agent with exact fixes — see [docs/PERMISSIONS.md](docs/PERMISSIONS.md) |
| **Rate limiting** | Per-tool sliding window (enforce mode), record-before-execute (TOCTOU-safe) |
| **Bot-to-bot loop detection** | Rate/repeat guard (5 exchanges / 60s, repeated acks) + per-channel chain breaker (6 consecutive bot turns, no human → quiet until a human posts) + `NO_REPLY` abstain sentinel |
| **Duplicate message suppression** | Per-channel dedup: identical content posted to a channel twice inside 120s is discarded |
| **Audit** | Every tool call, HITL decision, and auth event lands in a JSONL log |
| **SSRF** | App-layer URL validation: scheme whitelist, RFC1918/localhost/link-local block, DNS rebinding protection |
| **Path traversal** | Any tool writing to a user-controlled path runs `validate_file_path()` first |
| **SQL injection** | Parameterized queries everywhere |
| **Env isolation** | Only allowlisted env vars reach the Claude Code subprocess, so secrets can't leak |

### Content Safety Pipeline

`src/core/content_safety.py` implements a pipeline of three stages, applied to the output of **web-facing tools** — the ones that pull in external content where adversarial payloads might hide. The pipeline executes in `src/mcp_server.py` once a tool has finished.

**Which tools count as web-facing:** `web_search`, `browse_url`, `read_url`, `browser`, `gmail_read`, `gmail_search`, `download_file` — the list lives in `src/core/alerts.py:WEB_FACING_TOOLS`.

#### Stage 1: Sanitization

`sanitize(text)` — removes material that has no business entering the LLM context:

- Invisible Unicode: zero-width joiners, bidi overrides, BOM
- `<script>`/`<style>` blocks first, followed by every other HTML tag
- Unescaping of HTML entities
- Data URIs and any base64 run over 200 chars, replaced with `[base64-removed]`
- NFC normalization of Unicode
- Collapsed whitespace

**Currently log-only** — sanitization executes and fires an alert to the security channel whenever more than 50 chars would have been cut, yet the content flows on untouched. Running it this way surfaces the false-positive rate before active stripping gets switched on.

#### Stage 2: Injection Scoring

`score_injection(text)` — rates external content on a 0–10 scale for signs of prompt injection, using 23 regex patterns grouped into four categories:

| Category | Example patterns | Score per match |
|----------|-----------------|-----------------|
| **Instruction injection** | `ignore previous instructions`, `<\|im_start\|>`, `[INST]` | 3–4 |
| **Authority spoofing** | `emergency override`, `admin mode`, `debug mode:` | 2–3 |
| **Exfiltration** | `send this to`, `email it to`, `post in channel` | 2 |
| **Delimiter attacks** | `--- BEGIN SYSTEM`, `HUMAN:`, `ASSISTANT:` | 2–3 |

A **score of 3 or higher** triggers an alert carrying the tool name, the score, and which patterns hit. Only the first 50KB gets scanned, to keep it fast.

#### Stage 3: Behavioral Monitoring

`BehaviorMonitor` in `src/core/content_safety.py` — tracks how each agent acts over time and applies four anomaly checks:

| Check | Trigger | Severity |
|-------|---------|----------|
| **Sensitive after external** | A gated tool fires less than 5 min after external content was ingested | HIGH |
| **Novel tool** | A tool absent from the agent's baseline (its first 50 calls) | MEDIUM |
| **Volume spike** | Call volume hits 3× the rolling average over a 10 min window (at least 10 calls) | MEDIUM |
| **Rapid sensitive** | Three or more distinct sensitive tools inside 5 min | HIGH |

The behavioral monitor treats these tools as sensitive: `send_email`, `share_drive_file`, `install_mcp`, `team_add/remove/update`, `create_skill`, `send_message`.

### Security Alerts

`AlertSender` in `src/core/alerts.py` — pushes alerts to a Discord channel in real time over the REST API.

**Configuration** (Layer 3 `config.yaml`):

```yaml
security:
  alert_channel: "123456789"    # Discord channel ID for all security alerts
  alert_bot: "kbots"             # Bot account for sending (vault key: discord-{alert_bot})
```

Alerts only go out when both fields are present; drop either one and the fallback is a bare `logger.warning()`.

Make sure the alert bot can actually post in the alert channel. Its token is looked up in the vault under `discord-{alert_bot}`.

**What gets posted to the channel:**

- Content safety — an injection score of 3+ (with tool name, score, and matched patterns)
- Sanitization — more than 50 chars of invisible content removed (tool name and char count)
- Behavioral anomalies — any of the four checks above (agent ID, check type, severity, details)
- HITL outcomes — approvals, denials, and timeouts

### Permission Watch

`PermissionWatcher` in `src/core/permission_watch.py` — runtime counterpart to the boot preflight. Detects rights failures while the service runs and escalates them to a configured main agent, which verifies and reports to the owner with the exact fix commands and the access level required (SSH vs. interactive terminal vs. desktop vs. web dashboard).

**Detects:** unreadable Claude Code config (workspace trust broken — the "every tool denied" failure), tool-permission denials in the CLI event stream (`haven't granted it yet` — misconfiguration, not human rejection), and wrong file ownership via a periodic sweep that re-runs the preflight rights checks (catches damage done while agents are idle, on any OS, with no external timer machinery).

**Configuration** (Layer 3 `config.yaml`):

```yaml
security:
  permission_watch:
    enabled: true        # default true
    agent: ""            # main agent to brief (e.g. your coordinator)
    connector: discord
    channel: ""          # channel the briefing + owner report land in
    interval: 300        # seconds between sweeps
    cooldown: 3600       # per-issue re-report suppression
```

Escalation chain: configured agent → security alert channel → service log. Reports are deduplicated per issue via `cooldown`. Full guide: [docs/PERMISSIONS.md](docs/PERMISSIONS.md).

### HITL (Human-in-the-Loop)

Configured through the Layer 3 `config.yaml`. A gated tool call halts mid-flight while an approval request goes out to the designated Discord channel, where an approver settles it by reacting ✅ or ❌.

**Configuration:**

```yaml
security:
  hitl:
    connector: discord
    channel: "123456789"        # Channel for approval requests
    approvers: ["user_id"]      # Discord user IDs who can approve
    timeout: 1800               # Seconds before auto-deny (default: 30 min)
    fail_mode: closed           # "closed" = deny on timeout/error, "open" = allow
    poll_interval: 3            # Seconds between reaction checks
    gated_tools:                # Tools requiring approval
      - send_email
      - install_mcp
      - cloudflare_dns_update
      - team_add
      - team_update
      - team_remove
      - drive_delete
      - discord_delete_channel
```

A tool becomes gated through **either of two routes** — one is enough:
1. It appears in the `gated_tools` config list (Layer 3, so per deployment)
2. Its source carries `@tool(hitl=True)` (baked into Core for tools that are dangerous everywhere)

**Example flow:**

1. A user tells the agent: "Email the quarterly report to the client"
2. The agent invokes `send_email`; MCP middleware notices the tool is listed in `gated_tools`
3. The HITL channel receives: "🔒 **HITL Approval Required** — Agent `main` wants to call `send_email` with args: {to: ..., subject: ...}"
4. On a ✅ reaction the tool runs and its result flows back to the agent
5. On ❌ or timeout the tool yields a denial message, which the agent relays to the user
6. Either way, the decision is written to the audit trail and the alert channel

### Access Control

A three-tier scheme lives in `src/core/access_control.py`; each arriving message is evaluated on the combination of sender tier and agent tier.

**Sender tiers** (from `config/team.json`):
- `owner` — unrestricted; every tool available
- `admin` / `staff` — limited to safe tools, barring HITL approval
- `unknown` — turned away by default (`unknown_policy: deny`)

**Safe tools allowlist** — each deployment defines its own in Layer 3:

```yaml
security:
  access_control:
    safe_tools:
      - memory_search
      - web_search
      - team_list
      # ... read-only tools
    unknown_policy: deny
```

Anything missing from `safe_tools` demands owner tier — or, for agents, an HITL sign-off. The allowlist is a per-deployment decision, shaped by which tools exist there and which are appropriate.

### Rate Limiting

`src/core/rate_limiter.py` keeps a sliding window per tool and logs each call **before** it runs — closing the check-then-execute race, so it's TOCTOU-safe.

```yaml
security:
  rate_limits:
    mode: enforce               # "enforce" = block, "log" = warn only
    defaults:
      max_per_hour: 100
    tools:
      send_email:
        max_per_hour: 10
      install_mcp:
        max_per_day: 5
```

Patterns with wildcards also work:

```yaml
    tools:
      send_email:
        max_per_hour: 10
      install_mcp:
        max_per_day: 5
      amazon_sp_*:               # Matches all Amazon SP-API tools
        max_per_hour: 60
      trello_*:                  # Matches all Trello tools
        max_per_hour: 30
```

---

## Vault

Credentials sit Fernet-encrypted in `config/secrets.enc`, next to a per-instance random salt in `config/secrets.salt`. PBKDF2 (600k iterations for new vaults; older vaults are offered an in-place rekey by the wizard) turns the passphrase into the key; at startup the secrets are decrypted into memory. The passphrase itself is written to disk only if you accept the key-file prompt during setup (`~/.config/kbots-vault-key`, 0600) — the trade for unattended service start.

Manage via: `uv run python vault-manage.py`

**Where the vault file lives:** in the **overlay** — `$KBOTS_OVERLAY/config/secrets.enc` — like all deployment data. The path is resolved through the standard layer lookup (overlay → modules → core), so the core-repo location `<install>/config/secrets.enc` is only a fallback for installs that predate the overlay pattern. Both the service and `vault-manage.py` resolve the same way, which has one sharp edge: **if `KBOTS_OVERLAY` is not set in your shell, `vault-manage.py` falls back to a vault file inside the engine checkout that the running service never reads.** Secrets written there vanish into a stray `secrets.enc` and the bot keeps using the old values. Before managing secrets, confirm `echo $KBOTS_OVERLAY` prints your overlay path (the service units have it baked in; interactive shells get it from the profile exports `setup.py` writes). If you find a `secrets.enc` in the engine checkout on an overlay-based install, it's a stale pre-migration leftover — verify its keys exist in the overlay vault, then delete it.

---

## Memory System

Memory runs entirely in-process: SQLite with FTS5 full-text search plus BGE-small-en-v1.5 embeddings (384-dim, via ONNX), no external services involved. The database lives at `data/memory.db`.

| Feature | Implementation |
|---------|---------------|
| **Storage** | SQLite WAL mode, UUID primary keys |
| **Search** | FTS5 keyword search + embedding cosine similarity |
| **Scope** | Per-agent (`agent:<id>`), global, and group scopes; an agent sees only its own plus shared |
| **Context injection** | Session start injects pinned and high-confidence memories |
| **Audit trail** | Every insert/update/delete is recorded in the `changelog` table |
| **Dedup** | Semantic dedup kicks in at 0.80 similarity |

### Memory Lifecycle

A memory that goes unused loses confidence at 0.0108 per day.

```
Day 0:  0.70 confidence (new memory)
Day 10: 0.59
Day 30: 0.38
Day 60: 0.05 → archived (hidden from search)
+90 days archived → permanently deleted
```

**Pinning:** A pinned memory is exempt from decay altogether — it shows up in every relevant search and gets injected when a session starts. To pin one, pass `pinned=true` to `memory_store` at creation, or flip the pinned flag on an existing row directly in the memory database. Reserve pinning for facts that must never expire: system configuration, key contacts, core procedures.

**Accessing resets decay:** Each time a search surfaces a memory, its `last_accessed` timestamp is refreshed and the decay countdown starts over — so the memories that keep proving useful stick around longest.

### Memory Types

| Type | Purpose | Example |
|------|---------|---------|
| **semantic** | Facts, knowledge | "The client wants updates by email, not Slack" |
| **episodic** | Events, experiences | "v2.1 shipped March 15; auth bug forced a rollback" |
| **procedural** | How-to, processes | "Rotate API keys with vault-manage.py" |

---

## Context Compression

Agent context files (codex docs, skill prompts) can optionally pass through a rule-based compressor that trims clear-cut prose filler and leaves code blocks, config, paths, URLs, headings, and tables intact. It never touches an LLM — regex and heuristics only, finishing in milliseconds.

Batch compression skips agent identity files (AGENTS.md): those are hand-tuned agent prompts, and every word in them is deliberate.

**Configuration** (`config.yaml`):

```yaml
security:
  compression:
    enabled: false              # opt-in, disabled by default
    level: standard             # lite (filler only) | standard (filler + contractions)
    memory_recall: false        # compress memories at injection time
```

Individual agents can override this in `agents.yaml`:

```yaml
agents:
  my_agent:
    compression:
      enabled: true
      level: lite
```

**Levels:**
- `lite` — removes only filler phrases such as "please note that" or "it is important to"
- `standard` — filler removal plus contraction of phrases like "do not" → "don't"

**Tools:** `compress_context` supports lite/standard/report levels; `decompress_context` restores from the .original backup

**Batch:** `scripts/compress-context.sh [--dry-run] [--level lite|standard]`

**Implementation:** the engine is `src/lib/compressor.py`; `src/tools/compress.py` wraps it as an MCP tool

---

## Scheduled Tasks (systemd timers)

Every scheduled task rides on a systemd timer, with `Persistent=true` so runs missed during downtime fire after reboot. The templates live in `config/timers/`, and `setup.py` installs them automatically through `scripts/install-systemd.sh`.

| Timer | Schedule | Script | Purpose | Alerts |
|-------|----------|--------|---------|--------|
| kbots-memory-context | Every 30 min | regen-memory-context.sh | Rebuilds `MEMORY_CONTEXT.md` — memory stats plus a service-health snapshot — for injection into agent context | No |
| kbots-memory-decay | Daily 03:00 | memory-decay.sh | Runs the 0.0108/day confidence decay, moves below-threshold memories to the archive, and deletes archives past 90 days | On purge |
| kbots-health-audit | Every 12h | health-audit.sh | Audits the whole system — services, timers, resources, security, memory, vault, MCP, tools, databases, git state — and can be triggered on demand with the `/system-audit` slash command (direct execution, no LLM) | Success heartbeat; alert when issues found |
| kbots-integrity | Every 12h | monitor-integrity.sh | Compares SHA256 checksums of critical files against a baseline to catch unauthorized changes | On mismatch |

**Dependencies:** Every timer service declares `After=kbots.service` so nothing fires while the main service is still coming up. The memory-context timer additionally requires that the memory database (`data/memory.db`) already exists.

**Alerts:** When a script finds a problem it alerts Discord through `scripts/lib-alert.sh`, pulling the bot token out of the Fernet vault. Which channel receives the alert is set by `security.alert_channel` in `config.yaml`.

**Installation:** Unit files are rendered by `setup.py` into `<overlay>/systemd/` — paths substituted, `KBOTS_OVERLAY` injected — after which `scripts/install-systemd.sh` symlinks them under `/etc/systemd/system/` and enables the timers. There is no opt-out: the timers count as required infrastructure and are always installed.

**Adding custom timers:** Drop a `.service` + `.timer` pair into `config/timers/` (for Core) or `<overlay>/systemd/` (for one deployment), then run `scripts/install-systemd.sh <overlay-dir>`.

---

## Goal Workstreams

Long-running multi-agent collaborations toward a shared goal ("get 1000 streams for X"), spanning weeks or months and surviving restarts. A **goal** is a SQLite record (`data/goals.db`, WAL — opened directly by both the engine and the MCP tool subprocess, like the memory DB) anchored to one Discord channel that `goal_create` auto-creates. Store: `src/core/goals.py`; tools: `src/tools/goals.py`; config: the `goals:` section of `config.yaml`.

**Lifecycle:** `proposed → brainstorm → strategy → executing → done`, with `paused` and `blocked_on_user` side-states and `abandoned` as the exit. Transitions are validated; every write appends a `goal_events` audit row and bumps `last_activity_at`.

**Dynamic routing — no config edits:** participants of a live goal are routed into its channel straight from the goal store (`get_agent_for_channel` falls through to `goals.routed_participants_for_channel`), and they hear *every* message there (same semantics as `watch_channels`), with `NO_REPLY` as the abstain path. `goal_join` makes an agent live in the channel within ~10 s (TTL cache) — no restart. Each participating agent still needs its own bot account with guild access to the goals category (one Discord app per agent, as usual).

**Anti-ramble:** while a goal is in an active phase, its `turn_budget` (default 30) replaces the global `bot_chain_limit` in that channel; any human message resets the chain, and exhaustion posts one "waiting for a human check-in" notice and silences the channel. The repeat-message pair-mute (`_bot_loop_check`) is **never** waived. Soft norms ride in a `<goal-context>` block injected into every participating turn: phase, strategy, open tasks, open decisions, and phase-specific speaking rules (in `executing`, speak only when @mentioned, on a task event, or with new information — otherwise `NO_REPLY`).

**Facilitation:** the goal's owner agent advances phases (`goal_set`), assigns tasks (`goal_task`), and closes decisions. Any participant can propose a pause, abandon, or strategy change (`goal_propose` — a pause carries a wake condition); others support or object with reasons (`goal_vote`) during an objection window (default 24 h, closed by a scheduler wake to the owner); the owner rules with `goal_decide`. An adopted pause materializes its wake condition on existing machinery: `time` → a one-shot schedule, `metric` → a recurring watch schedule, `webhook` → a trigger (secret shown once), `email` → the owner's email watch plus the paused-goal context.

**Escalation:** when the team hits a wall only the user can clear, `goal_block` records a durable two-list brief (need-to-know / need-from-you), posts it in the goal channel with the escalation user's mention (config `goals.escalation_user`, default the first `access: owner` human in team.json), and optionally alerts `security.alert_channel`. The user replies in the channel; the owner calls `goal_resume`.

**Tier gating:** `goals.create_tiers` (default coordinator + privileged) controls who may create/advance goals; any agent may propose (goal parks at `proposed`), join, vote, and work tasks.

---

## Operations

### Start / Stop / Status
```bash
systemctl start kbots.service
systemctl stop kbots.service
systemctl status kbots.service
journalctl -u kbots -f
```

### Updating a Running Install

Whenever new code is pulled, `scripts/sync.sh` must run **before** the service restart:

```bash
cd "$KBOTS_HOME"
sudo -u kbots git pull
sudo -u kbots scripts/sync.sh              # install all layers
sudo systemctl restart kbots
```

The script first does `uv sync` for Core, then walks `KBOTS_MODULES` and `KBOTS_OVERLAY` to install any Layer 2 and Layer 3 `requirements.txt` files it finds. When those env vars are absent from the shell, it reads them out of the `kbots.service` systemd unit instead — the unit file is the single source of truth, and no exports in your shell profile are required.

Doing it in this order keeps Layer 2 packages alive through Core's dependency reconciliation; a bare `uv sync` would strip out any package it doesn't recognise.

Because the service unit launches with `uv run --no-sync`, starting the service never attempts to modify the sandboxed, read-only `.venv`. Dependency updates are therefore an **explicit** step: `scripts/sync.sh`, executed from an ordinary shell where `.venv` is writable, ahead of the restart.

Skip the sync after a dependency change and one of two things happens: startup dies with an `ImportError` on the new dep, or the service quietly keeps executing old code under a bumped version number. Since the script does nothing when there's nothing to do, running it on every single deploy costs nothing and is the safe habit.

### Git Workflow

A pre-commit hook — shipped as `hooks/pre-commit` and put in place by `setup.py` — rejects commits made straight to `main`. Every Core change travels through a feature branch:

```bash
git checkout -b fix/description
# make changes
git add <files> && git commit -m "fix: description"
git push -u origin fix/description
gh pr create
# cross-review → merge via GitHub
```

**Remote neutralisation:** On installs owned by non-maintainers, `origin` has been renamed to `upstream` and pushing is disabled (details under Three-Layer Architecture above); a maintainer regains push access by reversing the rename.

**Hook enforcement:** On each commit the hook inspects the branch name; a commit attempted on `main` fails with a formatted message that walks through the branch + PR workflow. It also reminds agents that files specific to a deployment have no place in Core.

Hooks go in automatically during the wizard's git-hooks step; on later runs the wizard diffs the shipped hooks against what's installed and offers an update when they've drifted.

### Test Mode
```bash
uv run python -m src.main --profile test
```

### Run E2E Tests
```bash
uv run python scripts/test-tools.py                    # all tests
uv run python scripts/test-tools.py --tool team_list   # single tool
```

### Vault Management
```bash
uv run python vault-manage.py
```

---

## Docs

| Document | Purpose |
|----------|---------|
| **ARCHITECTURE.md** | The document you're reading — the complete system reference |
| **SCRIPTS.md** | Every script, with usage examples |
| **skills/README.md** | Reference for the skill file format |
| **docs/PERMISSIONS.md** | Permissions & rights — per-platform setup, failure catalog, permission watch |
