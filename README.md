![kbots — one process · LLM-agnostic · trains itself](assets/banner.svg)

`v1 · MIT · Python 3.12`

kbots turns a chat server into a team of persistent AI agents. Each agent lives in its own project directory, thinks with the LLM you give it, remembers across sessions, and does real work through tools — all from a single Python process with SQLite underneath. Nothing else to run: no containers, no external databases, no queue.

```
Discord message → Router → Agent (project context + memory) → LLM → tools → reply
```

**First time here?** Jump to [Quickstart](#quickstart--zero-to-agent) — four steps, ~10 minutes, ends with an agent answering you in Discord. Hacking on the engine? See [Dev loop](#dev-loop).

## What you get

- **One process, one database** — everything runs in a single asyncio process on SQLite; there is no infrastructure layer to babysit
- **Trainable** — opt-in, captures every turn (prompt → full tool trace → response → 👍/👎 outcome) and exports to nanoGPT / MLX-LM / OpenAI / DPO-KTO to fine-tune a **local model** on the agents' own work
- **A real team, not one bot** — any number of agents, each with its own Discord identity, personality, permissions, and tool loadout
- **Memory that persists and improves** — SQLite FTS5 + semantic embeddings for recall, plus a lessons layer: agents record what worked, outcomes (👍/👎, corrections) adjust confidence, and a cheap-model reflector distills each agent's `LESSONS.md`, loaded at session start
- **Local models** — run agents on Ollama / LM Studio models (auto-detected), or enable the quality-first **tier router**: a tiny local model answers simple requests locally, Claude handles the rest
- **Create-then-operate** — Claude builds a tool + a scoped skill from a chat request (one HITL code review); from then on a **local model operates it** on demand/cron/webhook — or zero-LLM tool-direct — at zero subscription cost. See [docs/CREATE_THEN_OPERATE.md](docs/CREATE_THEN_OPERATE.md)
- **Automation built in** — agents schedule their own cron/interval/one-off tasks, and external systems (smart-home hubs, webhooks, CI) can fire agent actions through per-registration secrets
- **Self-healing deploys** — updates are test-gated with automatic rollback, and an independent watchdog restarts and rolls back the service if anything ever crash-loops it
- **Extend by dropping in a file** — a `@tool`-decorated async function becomes an agent tool on save; a YAML prompt file becomes a Discord slash command; both hot-reload with no restart. Agents can even write and promote their own tools
- **Approval gates where it counts** — sensitive tools pause for a human ✅/❌ in Discord, and the gate can be toggled live
- **Secrets stay encrypted** — a Fernet vault unlocked by passphrase at startup; credentials never sit in env vars or config files
- **Routing that fits a server** — send whole Discord categories, single channels, or mention-only traffic to different agents, with per-agent allow/deny lists down to individual tools
- **Sees and hears** — image/video analysis (Gemini) and audio transcription (Groq) in the message pipeline, plus a Playwright/Chromium `browse_url` tool for JS-heavy pages

## Quickstart — zero to agent

From clone to a Discord agent that answers you, in four steps. The wizard does the heavy lifting — this section tells you what to have ready.

> 🤖 **Are you a coding agent** (Claude Code or similar) installing kbots on macOS for a human? Skip the interactive wizard — follow **[docs/AGENT_INSTALL_MACOS.md](docs/AGENT_INSTALL_MACOS.md)** instead. It's written for you: you do the whole install, the human only handles Discord and auth approvals at your instruction.

### What you need

- [ ] **A machine** — Linux VPS (2 CPU, 4GB RAM) for production, or a Mac for local use
- [ ] **Python 3.12+** (the wizard installs `uv` and everything else itself)
- [ ] **An LLM engine** — kbots is engine-agnostic; agents pick a provider per agent:
  - **Claude Code CLI** (recommended, and what the setup wizard assumes for your first agent): a **Claude Pro or Max** subscription, no API key needed — Max has the higher usage limits if you'll run several agents. Pay-as-you-go **API/Console billing** also works (`claude auth login --console`).
  - **OpenAI Codex CLI**: a ChatGPT plan, `codex login` — set `llm.provider: codex_cli` on any agent.
  - **Any OpenAI-compatible endpoint**, including fully local models via Ollama / LM Studio ([docs/LOCAL_MODELS.md](docs/LOCAL_MODELS.md)) — usable per agent or as the tier router that keeps simple turns off your paid plan.
- [ ] **A Discord account** and a server where you have admin rights
- [ ] ~10 minutes

### Step 1 — create your Discord bot

Do this first, in a browser — the wizard will ask for the results.

> ⚡ **Shortcut: let Claude do the clicking.** If you have the [Claude in Chrome](https://claude.ai/chrome) extension (or Claude Code with browser control), paste this prompt and it walks the Developer Portal for you:
>
> ```
> Set up a Discord bot for me in the Discord Developer Portal
> (https://discord.com/developers/applications). I'm logged in.
>
> 1. Create a New Application named "<BOT NAME>".
> 2. On the Bot tab: enable the "Message Content Intent" and "Server Members
>    Intent" toggles under Privileged Gateway Intents, and save.
> 3. On the Bot tab, click "Reset Token" to generate a token, then STOP —
>    do NOT read, copy, store, or repeat the token. Leave it visible on
>    screen and tell me to copy it myself. Wait for me to confirm before
>    continuing.
> 4. Go to OAuth2 → URL Generator: select scopes "bot" and
>    "applications.commands"; under Bot Permissions select: View Channels,
>    Send Messages, Read Message History, Add Reactions, Attach Files,
>    Embed Links, and — for the MAIN agent — Manage Channels (so it can
>    create channels itself, e.g. the platform-updates notices channel).
>    Open the generated URL and help me add the bot to my server "<SERVER NAME>".
> 5. Then open https://discord.com/app, navigate into that server and into
>    the channel I want approvals in ("<CHANNEL NAME>"), and read the two
>    IDs straight from the address bar (discord.com/channels/<server id>/<channel id>).
>    Report them clearly labelled.
> 6. Finally tell me how to find my own user ID (Settings → Advanced →
>    Developer Mode, then right-click my name → Copy User ID) — don't try
>    to fetch it yourself.
>
> Give me a final summary: server ID, approvals channel ID, and a checklist
> confirming both intents are enabled and the bot has joined the server.
> ```
>
> Fill in `<BOT NAME>`, `<SERVER NAME>`, `<CHANNEL NAME>` first. The one thing Claude deliberately won't touch is the **token** — you copy that yourself in step 3, and it goes straight into the wizard's vault, nowhere else.

Prefer to do it by hand? Same steps:

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → name it (this becomes your agent's identity).
2. **Bot** tab → **Reset Token** → copy the token somewhere safe. *This is the one secret the wizard needs.*
3. Still on the Bot tab, under **Privileged Gateway Intents**, enable **Message Content Intent**. ⚠️ *This is the #1 setup mistake — without it the bot connects fine but silently ignores every message.* Enable **Server Members Intent** too while you're there.
4. Invite the bot: **OAuth2 → URL Generator** → scopes `bot` + `applications.commands` → permissions: *View Channels, Send Messages, Read Message History, Add Reactions, Attach Files, Embed Links* — plus **Manage Channels** for the **main agent** (so it can create channels itself, e.g. the platform-updates notices channel; sub-agent bots don't need it) → open the generated URL and add it to your server.
5. Collect three IDs (enable **Developer Mode** first: Discord Settings → Advanced → Developer Mode):
   - Right-click your **server** → Copy Server ID
   - Right-click **yourself** → Copy User ID (makes you the owner/admin)
   - Right-click the channel you want approval prompts in → Copy Channel ID

### Step 2 — clone and launch the wizard

```bash
git clone https://github.com/karvidsson/kbots.git
cd kbots
./setup
```

`./setup` installs uv if you do not have it, syncs dependencies, and starts the wizard. (Still prefer the pieces? `uv run python setup.py` after `bash scripts/sync.sh`.)

Three things worth knowing before you hit Enter:

- **Claude login:** the wizard checks `claude auth status`. If you're not logged in, it pauses and tells you to run `claude auth login` in another terminal (a Pro/Max plan, or `--console` for API billing), then continues.
- **Install directory:** the wizard clones the engine to `/opt/kbots` (Linux) or `~/kbots` (macOS). The checkout you just cloned stays **disconnected from the running service** — you can edit, break, and experiment in it without ever touching the live agent.
- **Overlay directory:** the wizard also creates an *overlay* next to the install (default: `<install>-overlay`, e.g. `/opt/kbots-overlay`). Your config, agents, encrypted vault, and data all live there — never in the engine checkout — so engine updates can't touch your deployment. Wherever this doc says `<overlay>`, that's the directory it means.

### Step 3 — answer the wizard

| The wizard asks for | Have ready from Step 1 |
|---|---|
| Overlay directory | Nothing — the default (a sibling of the install) is fine |
| Vault passphrase | Pick one — it encrypts your tokens at rest |
| Discord bot token | The token you copied |
| Server (guild) ID | The server ID |
| Your Discord user ID | Your user ID |
| Approvals channel ID | The channel ID (human-in-the-loop prompts land here) |
| Your name, role, timezone | Who the agents work for |
| Agent name, model, personality | Your first agent — defaults are fine |

Everything else (service install, browser tool) defaults sensibly. The wizard ends by **starting the service and waiting until your agent reports online**:

```
✓ MAIN AGENT ONLINE — mention @MAIN in Discord to talk to it
```

### Step 4 — say hello

In your Discord server:

```
@YourBot hello — who are you?
```

It should reply within seconds. Slash commands appear after a short sync — start with `/help`, which lists every command and skill live. That's it — you have a running agent with memory, tools, and an encrypted vault.

**The full command set:**

| Command | What it does |
|---|---|
| `/status` | Agent status and info |
| `/help` | List all commands and skills (always current — generated from the live command tree) |
| `/stop` | Stop the agent's current task |
| `/model [opus\|sonnet\|haiku\|default]` | View or change the LLM model for this agent |
| `/effort [low…max\|default]` | View or change the thinking effort level |
| `/session [info\|reset]` | View or reset the agent session |
| `/email-watch [seconds\|default]` | View or change how often this agent checks its email inbox |
| `/recall <query>` | Search agent memory |
| `/tools` | List tools available in this channel |
| `/hold` | Interrupt the agent so you can interject — it resumes with your message |
| `/schedule on\|off\|status` | Killswitch for scheduled tasks |
| `/schedules` | List scheduled tasks across agents, or cancel one |
| `/triggers on\|off\|status` | Killswitch for event-triggered automation |
| `/version` | Show the running platform version (and whether an update is pending) |
| `/admin restart\|pause\|resume` | Session/agent lifecycle (admin-only) |
| `/admin sync\|reload\|reboot` | Re-sync commands, hot-reload modules, restart the service (admin-only) |
| `/admin update` / `update-claude` | Pull the latest engine / update the Claude Code CLI (admin-only) |
| `/admin claude-auth status\|refresh` | Check/repair Claude login (admin-only) |
| `/admin hitl on\|off` | Toggle the approval gate (admin-only) |
| `/admin usage` | Show token consumption (admin-only) |

Skills also surface as slash commands automatically (`/debrief`, etc.) — `/help` lists whatever is installed.

**Interrupting a busy agent.** Sending a new message while the agent is mid-task does *not* interrupt it — messages queue per channel and run in order after the current turn finishes (you'll get a "👀 Noted" ack). The headless CLI can't steer a running turn, so the choice is queue or kill-and-resume. To cancel the current task and redirect: `/hold`, then send the new prompt — the process is terminated but the session is preserved, so the agent resumes with both its partial work and your new message in context. `/stop` is the same termination without the "interject now" framing. One caveat: messages already queued before the `/hold` still run first.

### If the bot doesn't answer

| Symptom | Likely cause | Fix |
|---|---|---|
| Connects but ignores all messages | **Message Content Intent off** | Developer Portal → Bot → enable it → restart the service |
| Ignores messages unless DM'd | Agent is mention-only (default) | `@mention` the bot, or set `mentions: false` in agents.yaml routing |
| Replies with an error / nothing, logs show Claude errors | Claude CLI auth expired | `/admin claude-auth refresh` in Discord; if that fails, `claude auth login` on the host, then `/admin reboot` |
| Service won't start | Config or token problem | Logs: `journalctl -u kbots -f` (Linux) / `tail -f <overlay>/data/launchd.stderr.log` (macOS) |
| `status=226/NAMESPACE`, restart loop (Linux) | A path in the unit's `ReadWritePaths` does not exist | systemd builds the sandbox before exec, so it never reaches the code that would create it. The log names the path: create it, `systemctl daemon-reload`, restart |
| Slash commands missing | Global sync takes time | `/admin sync` in Discord, or wait up to an hour |
| "Vault unlock failed" in logs | Key file missing/wrong | Re-run `uv run python setup.py` in the install dir — it re-prompts for the passphrase |

Manual controls, once running:

```bash
# Development (foreground, from the install dir)
uv run python -m src.main

# Linux service
sudo systemctl restart kbots.service
journalctl -u kbots -f

# macOS service
launchctl kickstart -k gui/$(id -u)/com.kbots.agent
tail -f <overlay>/data/launchd.stderr.log
```

> **Browser tool:** the `browse_url` tool needs a Chromium binary (~170MB). The wizard offers to install it, or run later: `uv run playwright install chromium`

### Your second agent

The wizard sets up the process and your **main agent** only. Further agents are created *by the main agent*: ask it in Discord ("create an agent called research that..."), approve the HITL prompt, then restart (`/admin reboot`). It scaffolds the agents.yaml entry, agent directory, AGENTS.md (+ CLAUDE.md stub), and permissions via its `create_agent` tool.

**Every agent gets its own Discord bot** — own name, avatar, and entry in the member list (one Discord app per agent; `create_agent` refuses to share a bot between agents). Discord has no API to create apps, so the main agent replies with a **pre-filled Claude-in-Chrome prompt** (bot name and server ID already inserted) that automates the portal setup. It registers the bot account in config automatically and tells you exactly where the token goes — your part is: paste the prompt, copy the token into the vault, restart.

### Dev loop

Developing the engine itself? Spin up a **disposable test agent** — isolated instance (own data dir, own lock, a live install is untouched), terminal chat, and full teardown when you exit:

```bash
uv run python scripts/dev.py chat            # real Claude — chat with TESTBOT in the terminal
uv run python scripts/dev.py chat --mock     # offline mock LLM — zero quota, tests the pipeline
uv run python scripts/dev.py chat --keep     # preserve the .dev/ workspace between sessions
uv run python scripts/dev.py chat --agent-prompt "You are testing feature X..."
uv run python scripts/dev.py down            # force-stop + remove leftovers
uv run python scripts/dev.py check           # ruff + pytest
```

Type `/quit` (or Ctrl+D) to exit — the test agent is stopped and removed automatically. Everything lives in a gitignored `.dev/` directory while the session runs.

### Post-install

Anything the wizard configured can be changed later without re-running it — `settings.py` is a TUI over the whole config surface (LLM defaults, connectors, memory, HITL gates, rate limits, agents, timers, vault secrets, …):

```bash
uv run python scripts/settings.py
```

[SCRIPTS.md](SCRIPTS.md) lists the full menu.

### Updating

One command, in the install directory (or `/admin update` from Discord):

```bash
cd "$KBOTS_HOME"                 # KBOTS_HOME = the install dir:
                                   # /opt/kbots (Linux) or ~/kbots (macOS).
                                   # Set in the service units; if your shell
                                   # doesn't have it, just cd to the path.
scripts/update.sh
```

It pulls, syncs dependencies across all layers, then classifies the changes: **tools/skills/codex changes hot-reload live** (the engine's file watcher picks them up — no restart), anything touching core code, dependencies, or units triggers a service restart (systemd or launchd automatically).

> **Warning:** a bare `git pull` + restart is not an update. Because the service launches with `uv run --no-sync`, dependencies are only ever installed by the sync step — skip it and the next restart either crashes on a missing package or quietly keeps running old code.

### Moving to another machine

Your whole deployment lives in the overlay (config, vault, agents, memory), so moving it is: **export → copy → import**. Absolute paths are rewritten for the new machine automatically.

```bash
# On the old machine — bundle the overlay (+ optional vault key):
uv run python scripts/migrate.py export --overlay <overlay> --out ~ [--with-key]

# Copy kbots-export-*.tar.gz to the new machine, clone the engine there, then:
uv run python scripts/migrate.py import kbots-export-*.tar.gz \
    --overlay <new-overlay> --engine <new-engine-root>
```

Import restores the overlay, rewrites the baked-in paths (engine root, overlay, home), and restores the vault key if you bundled it (otherwise you re-enter your passphrase — your `secrets.enc` travels with the overlay). Then set `KBOTS_OVERLAY` and run `setup.py` to install the service, or launch the engine directly. Regenerable junk (embedding model, tmp, logs) is excluded from the bundle; the SQLite memory DB is kept.

### Uninstalling

```bash
uv run python uninstall.py                # offers export first, then removes
uv run python uninstall.py --export-only  # just make a portable bundle
```

The uninstall wizard detects the install (overlay, engine clone, service, sudo rule, shell env, vault key), **offers to export it first** (so you can move it), then — after confirmation — stops and removes the service, revokes passwordless sudo, strips the shell-profile exports, and deletes the overlay, engine clone, and vault key. It leaves the engine *source checkout* in place.

### File ownership

The service runs as the `kbots` user, and it must own every file in the install tree. Editing as root silently flips ownership on the files you touch, which later breaks `uv sync` and leaves the service unable to write its own files — so always `chown kbots:kbots` anything you edited as root. To audit the tree:

```bash
find "$KBOTS_HOME" -not -user kbots 2>/dev/null   # any output = files to chown back
```

The same rule covers the service user's `~/.claude.json` and `~/.claude/` — if those go root-owned (typically from running `claude` as root), headless workspace trust breaks and **every agent loses every tool**. Full per-platform setup (Linux / macOS / Windows-WSL2), the failure catalog, and the runtime watchdog that escalates these to your main agent: **[docs/PERMISSIONS.md](docs/PERMISSIONS.md)**.

### Profiles

A named profile loads `config.<profile>.yaml` and `agents.<profile>.yaml` instead of the defaults — handy for running a test instance beside production:

```bash
uv run python -m src.main                  # default profile
uv run python -m src.main --profile test
```

## Tools — a decorated function

A tool is one async Python function with a `@tool` decorator. The schema the LLM sees is generated from the type hints; `ctx` hands the function the vault, memory, and channel it's running in:

```python
# src/tools/my_tool.py
from src.core.base import ToolContext
from src.core.tools import tool

@tool(name="check_weather", description="Get weather for a city")
async def check_weather(ctx: ToolContext, city: str) -> str:
    api_key = ctx.vault.get("weather-api-key")   # encrypted vault access
    return f"Weather in {city}: sunny, 25C"
```

Save the file under `src/tools/` and it's live — auto-discovery surfaces it to every agent over MCP with nothing to register or import.

**Agents can write their own tools too.** Every agent has a `create_tool` — it submits Python source, the code is statically checked (no subprocess/eval/os.system/…), and the **full source lands in your Discord approval prompt** — the HITL approval *is* the code review. On approval the tool is written to the overlay and hot-loaded live for all agents, no restart. (`create_skill` does the same for YAML prompt-skills, no approval needed since skills are declarative.)

## Skills — a YAML file becomes a slash command

A skill is a prompt template plus the tools it may use, written in YAML — no code. Every skill automatically registers as a Discord slash command, parameters included:

```yaml
# skills/my_skill.yaml
name: daily_report
description: Generate a daily business report
parameters:
  - name: focus
    type: string
    choices: [sales, marketing, operations]
prompt: |
  Generate a {focus} report for today. Check recent data and summarize key metrics.
tools:
  - web_search
  - memory_search
```

A skill doesn't have to involve an LLM at all — give it a `command` instead of a `prompt` and invoking it runs the shell command directly:

```yaml
name: system_audit
description: Run a full system health audit
command: scripts/health-audit.sh --report
```

The full format, including direct command execution, is in [skills/README.md](skills/README.md).

## Event triggers (webhooks)

Agents can react to **external events** — "when X happens, do Y". Enable the inbound webhook connector, and an agent registers a trigger that fires when a matching event POSTs in.

```yaml
# config.yaml
connectors:
  webhook:
    enabled: true
    host: "127.0.0.1"   # localhost only by default; "0.0.0.0" for LAN (secure it!)
    port: 8090
```

The flow, in plain language:

1. You tell an agent: *"when the kitchen light goes off, turn on the hallway light."*
2. It calls `create_trigger` (HITL-gated) and hands you a **unique URL + secret for that one registration** to configure in your device/hub:
   ```bash
   curl -X POST http://127.0.0.1:8090/event/t1 \
     -H 'X-Webhook-Secret: <this-trigger-secret>' \
     -d '{}'
   ```
3. Point your smart-home hub (Home Assistant, Shelly, …), cron, CI, or an external SaaS webhook at that URL.
4. When it fires, the engine re-invokes the agent with your instruction (plus any JSON you sent as event data). The agent reasons and acts, replying in the channel where you set it up.

**Security model** — this is an inbound path into a capable agent, so it's locked down:
- **Per-registration secrets.** Each trigger gets its own secret and URL (`/event/<id>`). Only the SHA-256 *hash* is stored (the plaintext is shown once), and a leaked secret can fire **only that one trigger** — not the others. So if the agent registers with several external providers, each gets an isolated secret.
- **Global killswitch.** `/admin triggers off` instantly stops **all** event-triggered inference — incoming events are accepted but no agent runs (triggers are paused, not deleted). `/admin triggers on` re-enables; `status` shows state + count.
- **Localhost by default**, gated creation (`create_trigger` needs HITL approval), and constant-time secret checks. Manage with `list_triggers` / `delete_trigger`.

Pairs with the scheduler below for time-based automation.

## Scheduled tasks

The time-based companion to event triggers: an agent can **schedule its own tasks**. Ask it — *"every morning at 8, check the news and summarise it here"* — and it calls `schedule_task`. When due, the engine re-invokes the agent in that channel with the instruction, so it runs with full tools/memory and replies. Nothing to enable; it's built in.

Timings (server local time), pick one:
- `cron='0 8 * * *'` — recurring cron (daily 8am; weekdays 9am → `0 9 * * 1-5`; hourly → `0 * * * *`)
- `every_minutes=120` — repeat every N minutes
- `in_minutes=30` — once, N minutes from now ("remind me in 30")
- `once_at='2026-07-05 09:00'` — once, at a specific local time

Manage with `list_schedules` / `cancel_schedule`. Pause everything instantly with **`/admin schedule off`** (schedules are kept, just not fired) — the global killswitch, same as `/admin triggers` for events.

## Working memory (lessons)

Agents get **smarter over time** via a reward-based lessons layer built on the existing memory. The novel bit is an **outcome signal** — memory now knows what actually *helped*, not just what's recent.

- **Capture:** an agent calls `remember_lesson(...)` to save a durable insight, a dead end, or a correction.
- **Reward:** `record_outcome(lesson, "useful" | "dead_end" | "corrected")` moves the lesson's confidence up or down. A user correction is high-signal — the agent records it with the fix. You can also just **react 👍/👎 on a reply** — it nudges the confidence of the lessons that reply drew on.
- **Surface:** the existing machinery does the rest — a lesson that keeps helping crosses the `0.8` auto-inject line; a dead end sinks and decays out.
- **Consolidate:** a **reflector** runs periodically (cheap model, **Haiku** by default — a single focused call, not a full agent turn) to dedupe and group each agent's lessons into a per-agent `LESSONS.md`.
- **Inject:** `build_startup_context` reads `LESSONS.md` into a `<lessons>` block at the **start of each session** (once per CLI session), so the agent begins already knowing what worked and what to avoid.

Configured under `defaults.memory.reflection` (`enabled`, `model`, `interval_hours`); run it on demand with the `reflect` skill. It reuses the SQLite store, BGE embeddings, confidence/decay, and session-start injection that already existed — no parallel memory system.

## Codex — knowledge agents can't fetch

Some knowledge lives in no API: your brand voice, company profile, supplier contacts, standard procedures, strategy notes. That goes in `codex/`, organized however suits the business:

```
codex/
├── _index.md          ← master index — agents read this first
├── business/          ← brand voice, compliance
├── products/          ← product info, guidelines
├── strategy/          ← playbooks, competitor analysis
└── processes/         ← SOPs, workflows
```

An agent's AGENTS.md points it at the codex, and `_index.md` draws the line between what to read here and what to fetch live from tools — so agents never quote a stale doc when fresher data is a tool call away. Full guide: [codex/README.md](codex/README.md).

## Built-in tools

| Category | File | Tools | Requires |
|----------|------|-------|----------|
| **Memory** | memory.py | store, search, semantic_search, forget | Built-in |
| **Team** | team.py | list, get, add, update, remove | Built-in |
| **Web** | web_search.py | search | Tavily API key |
| **Discord** | discord_tools.py | read_channel, read_message, search, send_file | Discord bot token |
| **Media** | audio.py, video.py | transcription, frame/clip extraction | Groq API key (audio) |
| **Browser** | ingest.py | read_url, browse_url (headless Chromium via Playwright) | `playwright install chromium` |
| **System** | ingest.py, tmux.py, builtin.py | skill creation, tmux, inter-agent messaging | Built-in |

Everything marked **Built-in** works on day one — API-key tools can be enabled any time by adding the key to the vault (`uv run python vault-manage.py`). Remove a file = remove those tools.

**Vendor integrations live in [`extras/`](extras/README.md)** — Google Workspace, Trello, Notion, GitHub, Cloudflare, Gemini media, stocks, news, monitoring, Shelly smart-home. They are *not* auto-loaded; install one with a single `cp` into your overlay's `tools/` dir (each extra's README has the exact line). Add your own integrations the same way — a `@tool` decorated Python file in `$KBOTS_OVERLAY/tools/`, or let an agent write it live with `create_tool`.

## Architecture

One process, with every layer swappable:

```
                        ┌─ the kbots process ──────────────────────────┐
 Discord ───────────────►  connector                                   │
 (Slack: roadmap)       │      │                                       │
                        │   router ── channel/category → agent         │
                        │      │                                       │
                        │   access control (3 layers)                  │
                        │      │                                       │
                        │   agent ── project dir · memory · session    │
                        │      │                                       │
                        │   LLM (Claude Code CLI subprocess / local)   │
                        │      │                                       │
                        │   MCP middleware: rate limit → HITL gate     │
                        │        → execute tool → audit log            │
                        │                                              │
                        │  Fernet vault · SQLite memory · hot reload   │
                        └──────────────────────────────────────────────┘
```

Connectors, LLM providers, memory backends, tools, and skills are all auto-discovered modules — implementing one means dropping a file in the right folder and naming it in config.

## Running more than one agent

Agents are YAML blocks. Each one picks its bot identity, model, tool loadout, and where in the server it listens:

```yaml
# config/agents.yaml
agents:
  main:
    display_name: "My Agent"
    llm:
      provider: claude_code
      model: opus
    tools: all                    # Full MCP tool access
    disallow_builtins:            # Block file editing and shell access
      - Edit
      - Write
      - Bash
      - MultiEdit
    routing:
      discord:
        account: main             # Uses the 'main' bot
        channels: []              # All channels (wildcard)
        mentions: true

  assistant:
    display_name: "Helper"
    llm:
      provider: claude_code
      model: sonnet
      timeout: 1800
    tools:                        # Restricted tool list
      - memory_search
      - web_search
      - trello_boards
      - trello_cards
    routing:
      discord:
        account: helper           # Separate bot identity
        categories: ["123456"]    # Only responds in this category
        mentions: true
```

When routing rules overlap, the agent with the more specific match (channel or category) wins over a wildcard agent.

## Access control

Permissions are decided three times before a tool ever runs, each layer narrowing the last:

| Layer | Question it answers | Configured in |
|-------|---------------------|---------------|
| Conversation | May this sender talk to this agent at all? (sender tier × agent tier) | `config/team.json` |
| Per-message | Which tools does *this sender* get for *this message*? | `config.yaml` |
| Agent ceiling | What is this agent ever allowed to use, regardless of sender? | `agents.yaml` |

Senders come in four tiers — **owner** (everything) → **admin** (safe tools + HITL approval rights) → **staff** (assistant agents only) → **unknown** (no access).

**Workspace isolation (agent tiers):** every agent runs in its own project directory. The **main agent (coordinator)** can read all agents' work folders — it's the orchestrator. **Assistants** are confined to their own folder (`Read(./**)`) and cannot touch each other's workspaces, and only a coordinator or privileged agent can use admin tools like `create_agent`. **Privileged** agents (rescue/ops) bypass isolation entirely. The engine additionally refuses configs where one non-privileged agent's directory contains another's.

### Full machine control

By default the main agent has **no shell at all** — it reads files and calls tools, but can't run commands. You can give it full control of the machine, chosen in the wizard (Step 10) or changed later via `scripts/settings.py` → *Main Agent — Machine Control*:

| Level | What the agent can do |
|-------|-----------------------|
| **none** (default) | Read + tools only. No shell. |
| **user** | Full shell + file access as the service account (everything you can do without a password). Runs `brew`, manages files, executes scripts. |
| **root** | The above **plus passwordless sudo** — literally anything as root. Installs a `sudoers.d` rule (asks your password once); revoke with `scripts/full-control.sh revoke`. |

Full control makes the main agent a **privileged** agent (`Bash(*)`, no builtin blocks). Pair it with the approval gate as you see fit:

- **Approval gate (HITL):** keep it **on** and the agent still asks you in Discord before sensitive/destructive actions — full capability, human in the loop. Turn it **off** for full autonomy (true "skip permissions"). Decide the default in the wizard, then flip it live any time — either the **`/admin hitl on|off`** slash command, or just **tell the agent** ("turn off approvals"), which calls the `set_hitl` tool. Both are **admin-only** (only a configured admin can flip it — an agent can't silently disable its own oversight), take effect on the next message, and persist across restarts.

> Full control means the agent can run anything you can — only enable it on a machine you trust to your agent. `root` level with the gate off is maximum power and maximum risk.

## LLM engine

The primary engine is the **Claude Code CLI**, authenticated with your Claude subscription (**Pro or Max** — no per-token API costs) or with pay-as-you-go API/Console billing. Max is recommended for multi-agent setups thanks to higher usage limits. Each agent runs as its own Claude Code subprocess that:

- reaches the whole toolset over MCP (Model Context Protocol)
- resumes its session between conversations, so context carries over
- picks its model per agent — `opus`/`sonnet`/`haiku` aliases resolve to whatever version the CLI currently ships
- gets its own configurable timeout

**Model names are dynamic.** Agents are configured with aliases (`opus`/`sonnet`/`haiku`); the Claude Code CLI resolves each to the latest version, so `/admin update-claude` is all it takes to move to newer models — nothing is pinned in code. An explicit model id in config passes through verbatim if you want to pin one.

**Usage-limit resilience.** When a conversation hits a per-model usage cap, the engine **auto-downgrades** to a cheaper model (opus → sonnet → haiku) to keep you going, pins that conversation to the fallback for the rolling-limit window (~5h), then reverts automatically. It posts a one-time alert to your ops channel when this happens, and a `🛑` alert if even the cheapest model is capped. Check consumption any time with **`/admin usage`** (per-agent token totals for today / 7 days, plus current tool-call rates).

**Zero-install CLI deps (pkgx).** External binaries the media/system tools need (ffmpeg, tmux, …) resolve at call time: PATH first, then — if the optional [pkgx](https://pkgx.sh) (4MiB) is installed — via `pkgx -q <tool>`, fetched and cached on first use with no sudo and nothing installed system-wide. Agents' Bash sessions can likewise `pkgx <tool>` for one-off CLIs instead of brew/apt installs. Without pkgx, missing binaries return a clean install hint instead of raw errors.

**Local models.** Agents can also run on local models via **Ollama or LM Studio** (`llm: {provider: local, model: qwen3.5:9b}`) — both speak the same OpenAI-compatible API and are auto-detected. Local agents get the full toolset (rate-limits, access control, and HITL enforced by the engine). An optional **tier router** puts a tiny local model in front of Claude: clearly-simple requests are answered by a local workhorse model, everything uncertain escalates to Claude (quality-first) — cutting subscription usage without giving up Claude-level answers. The setup wizard detects your RAM and picks matching models. See [docs/LOCAL_MODELS.md](docs/LOCAL_MODELS.md).

## Memory

Memory is a single SQLite database (WAL mode) with two search paths over it: an FTS5 full-text index and semantic search via BGE-small-en-v1.5 embeddings (384-dim, ONNX — no external service). Each agent sees only its own memories plus an explicitly shared scope; relevant memories are recalled automatically at session start. Unused memories decay, then archive, then purge — unless pinned, which exempts them entirely.

## Configuration

The setup wizard (`uv run python setup.py`) writes every config file interactively, and `uv run python scripts/settings.py` edits any of it afterwards. Prefer doing it by hand? Copy the templates and fill them in:

```bash
cp config/config.yaml.example config/config.yaml
cp config/agents.yaml.example config/agents.yaml
cp config/team.json.example config/team.json
```

Real config never gets committed — the non-`.example` files are all gitignored, so deployment details stay out of the repo.

## Everyday commands

```bash
# Run
uv run python -m src.main

# Run with profile
uv run python -m src.main --profile test

# Setup wizard
uv run python setup.py

# Settings manager (post-install)
uv run python scripts/settings.py

# Vault management
uv run python vault-manage.py

# Update a running install (hot-reloads or restarts as needed)
scripts/update.sh

# Run tool tests
uv run python scripts/test-tools.py
```

## Self-development — agents that build the platform

A **privileged ops agent** (e.g. an "engineer" bot with `Bash(*)` + the machine's own git/GitHub credentials) can extend kbots itself — write engine code, tools, and skills — and ship it, *safely*. The pieces fit together as a self-improving loop with guardrails at every step:

1. **Build** — the ops agent edits the source repo, runs the tests, and commits to a **feature branch** (direct commits to `main` are blocked), then opens a PR.
2. **Review** — the cross-review rule holds: a **human approves the merge**. An agent can prepare and gate-check a PR but not self-merge.
3. **Deploy safely** — instead of `update.sh`, the ops agent runs **`scripts/self-deploy.sh`**: pull → sync → **gate on `ruff` + full `pytest`** → restart → **health-check the boot** → **auto-rollback to the previous commit** if tests fail or the service doesn't come up clean. The box is never left on broken code.
4. **Self-heal** — an independent **watchdog** (`scripts/install-watchdog.sh`) runs as its own service, watches the engine's heartbeat, and **auto-rolls-back + restarts** if anything ever crash-loops the service — the lifeboat for the case a deploy's own rollback didn't catch. Verified end-to-end on both macOS/launchd and Linux/systemd.

```bash
# The ops agent's safe deploy loop (deterministic — safety lives in the script, not the agent):
cd "$KBOTS_HOME" && scripts/self-deploy.sh   # test-gated deploy + auto-rollback
scripts/install-watchdog.sh                     # one-time: install the self-healing watchdog
```

Layered with **HITL** (sensitive tools need Discord approval, toggleable live), the test gate, PR review, and the watchdog, this lets agents genuinely develop the platform without the risk of an agent bricking the service it runs on. See [ARCHITECTURE.md](ARCHITECTURE.md) and [SCRIPTS.md](SCRIPTS.md).

## Training — fine-tune a local model on the agents' work

Opt-in, kbots records its own work as a training dataset — so you can eventually run a **local model** that imitates (or, with a capable enough base, actually *performs*) what the agents do. Turn it on in `config.yaml`:

```yaml
kbots:
  training_collection: { enabled: true, include_tool_trace: true }
```

Every turn is captured to `data/training/turns.jsonl` — the assembled prompt, the **full tool-call trace** (recovered from Claude Code's own transcript), the final response, and outcome signals; 👍/👎 reactions land in `rewards.jsonl` as reward labels. Secrets are redacted; it stays local. `/admin training` prints the **resolved** path and how much of each you actually have — worth running first, since `data_dir` is often relative and resolves against the service's working directory, not wherever you are.

Note that `rewards.jsonl` is only created by the *first* 👍/👎: until someone reacts, reward-based filters have nothing to filter on and quietly fall back to outcome signals. Export to whichever trainer you want:

```bash
uv run python scripts/export_training_data.py --dir data/training \
    --format nanogpt sft mlx openai dpo --positive-only
```

| `--format` | Output | Train with |
|---|---|---|
| `mlx` | `train.jsonl` / `valid.jsonl` | **MLX-LM LoRA on Apple Silicon — no GPU** |
| `openai` | `openai.jsonl` (tool-call schema) | OpenAI / Together / Fireworks hosted fine-tune |
| `dpo` | `preference.jsonl` + `dpo_pairs.jsonl` | trl **KTO** / **DPO** (uses your 👍/👎) |
| `sft` / `nanogpt` | `sft.jsonl` / `corpus.txt` | Unsloth / Axolotl / trl, or the nanoGPT toy |

The exported `messages` preserve `user → assistant(tool_calls) → tool → assistant`, so a fine-tuned model can learn to **use the tools** — and since kbots is LLM-agnostic, you drop it in as a new `src/llm/` provider and it inherits the whole toolset. Reality check: this is about *accumulating* data over time — a fresh install has almost none, and nanoGPT-scale is a style mimic, not a worker. See [docs/TRAINING.md](docs/TRAINING.md).

## Security

- **Credentials** live in a Fernet vault (AES-128-CBC at rest, PBKDF2 with 100k iterations) unlocked by passphrase at startup
- **Dangerous tools** pause behind per-tool HITL gates — a human approves via Discord reaction, with a timeout
- **Access** is filtered three times per message: sender×agent tiers, per-sender tool restriction, static per-agent ceiling
- **Runaway behavior** is capped by per-tool/per-agent sliding-window rate limits, agent-to-agent loop detection, and dedup of identical messages to a channel within 120s
- **Everything is auditable** — every tool call, HITL decision, and auth event lands in a JSONL audit log
- **Memory is compartmentalized** — an agent reads only its own memories plus the shared scope

## Engine vs. deployment

The engine checkout (this repo) never contains anything of yours. Everything deployment-specific — config, agents, custom tools, vault, data — lives in an *overlay* directory that `setup.py` creates beside the install (default `<install>-overlay`, e.g. `/opt/kbots-overlay`; pick another path in the wizard if you like). See [ARCHITECTURE.md → Deployment Patterns](ARCHITECTURE.md#deployment-patterns) for the overlay's directory tree.

**Two-layer** (a single deployment):
```
Core:    /opt/kbots              ← engine (this repo)
Overlay: /opt/kbots-myproject    ← config, agents, custom tools, data (your private repo)
```

**Three-layer** (several deployments sharing domain tools):
```
Core:    /opt/kbots              ← engine (this repo)
Modules: /opt/kbots-modules      ← shared domain tools/skills (private repo)
Overlay: /opt/kbots-deploy-a     ← this deployment's config + agents (private repo)
```

Both patterns are wizard-supported and wired through two env vars: `KBOTS_OVERLAY` (path to the overlay — always set, by `setup.py`) and `KBOTS_MODULES` (colon-separated module directories, only for three-layer).

The payoff of keeping Core free of personal data, real config, domain tools, and agent identities (it ships `.example` templates only) is that `git pull` can never conflict with your deployment — updating the engine stays a non-event. The full rules: [ARCHITECTURE.md → Keeping Core Clean](ARCHITECTURE.md#keeping-core-clean).

## How kbots compares

"Which agent framework?" is two different questions. For *a running assistant on your own hardware, reachable from chat*, you're choosing a **harness** — kbots' peers are [OpenClaw](https://github.com/openclaw/openclaw), [Hermes Agent](https://github.com/NousResearch/hermes-agent), and [Letta](https://github.com/letta-ai/letta). For *agents built into your own product*, you're choosing a **framework** — [Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/), [LangGraph](https://github.com/langchain-ai/langgraph), [CrewAI](https://github.com/crewAIInc/crewAI), or the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python). Comparison as of **August 2026**.

### The harnesses (ready-to-run)

| | **kbots** | **OpenClaw** | **Hermes Agent** | **Letta** |
|---|---|---|---|---|
| One-liner | messaging → persistent LLM sessions in project contexts | personal assistant, any OS, any platform | self-improving harness by Nous Research | memory-first agent server (ex-MemGPT) |
| Runtime | Python 3.12, **one process + SQLite** | Node 24+, TypeScript | Python 3.11 | Python server; Docker, Postgres/SQLite |
| Chat channels | Discord (Slack on roadmap) | **11+** (Discord, WhatsApp, Signal, iMessage, Teams, Telegram…) + plugin channels | Telegram, Discord, Slack, WhatsApp, Signal + CLI | Slack, CLI, desktop app, SDK |
| LLM support | Claude Code CLI, any OpenAI-compatible endpoint, **local models** (Ollama/LM Studio auto-detect) | model-agnostic, bring your own key | Nous Portal, OpenRouter (200+), OpenAI-compatible, local vLLM | model-agnostic; Anthropic/OpenAI recommended |
| Memory | SQLite FTS + semantic search + agent-curated lessons w/ periodic reflection | session-scoped (per agent/workspace/sender) | FTS5 session search + LLM summarization + agent-curated memory | **the** differentiator — self-improving memory hierarchy (MemGPT lineage) |
| Agents create their own tools | ✅ `create_tool`: AST-validated, hot-loaded, **private to creator** until human-approved promotion | ➖ skills ecosystem ([ClawHub](https://clawhub.ai)), install-based | ✅ autonomous skill creation ([SKILL.md](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills), self-improving) | ➖ custom tools via SDK |
| Tool sandboxing | ❌ **in-process** — AST validation is a filter, not a boundary | ➖ allowlists; not detailed | ✅ 6 execution backends (local, Docker, SSH, Singularity, Modal, Daytona), container hardening | ✅ dedicated tool sandbox |
| Human-in-the-loop | ✅ per-tool approval gates in chat, fail-closed, live-toggleable | ➖ allowlist config | ✅ command approval, DM pairing | ➖ |
| Secrets | ✅ **encrypted vault** (Fernet), passphrase at startup, never in env/config | env/config based | allowlisted secrets import | env/config based |
| Scheduling & triggers | cron-style schedules + inbound webhooks + **tool-direct actions (zero LLM calls)** | cron + webhooks | cron scheduler | via SDK |
| MCP | ✅ auto-surfaces as native tools | ➖ (plugin-dependent) | ✅ | ✅ remote + local |
| Multi-agent | ✅ config-defined agents, message bus, tier-based access control | ✅ session isolation per agent | ✅ parallel sub-agents | ✅ subagents |
| Train on your own data | ✅ **turn collection → export → fine-tune per-tool local specialists** | ❌ | ❌ (improves via skills, not weights) | ❌ |
| External services required | **none** — no Docker, no external DB, no cloud | none required | none required | Docker + DB for self-host; cloud offering |
| License / scale | MIT / young project, measured in users-per-install not stars | MIT / **~386k★**, hundreds of contributors, OpenClaw Foundation | MIT / **~228k★** | Apache-2.0 / ~24k★ |

### The frameworks (build-your-own)

| Framework | What it is | Choose it over a harness when… |
|---|---|---|
| [Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/) (MIT, ~12.5k★, .NET/Python, 1.0 Apr 2026) | AutoGen + Semantic Kernel unified by their own teams. Agents + graph workflows + an opinionated "[Harness](https://learn.microsoft.com/en-us/agent-framework/agents/harness)" component (planning, context compaction, don't-ask-again approvals) | you're shipping agents inside a .NET/Azure product with enterprise telemetry requirements |
| [LangGraph](https://github.com/langchain-ai/langgraph) (MIT, ~39k★, Python) | low-level orchestration for long-running stateful agents — durable execution, checkpointing, HITL state inspection. Klarna/Replit/Elastic in production | you need explicit control over a complex agent graph and will build the product around it |
| [CrewAI](https://github.com/crewAIInc/crewAI) (MIT, ~57k★, Python) | role-based agent teams ("crews") + event-driven "flows"; model-agnostic incl. local models | your problem decomposes into collaborating role-specialists inside your own app |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) (MIT, ~28k★, Python) | lightweight multi-agent library — handoffs, guardrails, sessions, tracing; 100+ models via LiteLLM; MCP | you want the thinnest possible first-party loop and will host everything yourself |

A framework gives you primitives and a blank `main.py`; a harness gives you a running system and takes opinions in exchange. kbots' opinion: **one process, one database, chat as the UI, everything else is a module.**

### Where kbots loses, plainly

No sandbox for in-process tool execution (validation is a filter, not a boundary). One chat connector today. Single-node by design. No GUI, no desktop app, no hosted option. A community you could fit in a phone booth. If any of those is disqualifying, the projects above are genuinely good.

### Where kbots wins

- **Zero-maintenance ops** — one process + SQLite; nothing else to run, back up, or upgrade
- **Encrypted credential vault** (Fernet, passphrase-unlocked) instead of secrets in env vars
- **HITL approval gates** in the chat you already use — per-tool, fail-closed, live-toggleable
- **Agents write their own tools**, private to their creator until human-approved promotion
- **LLM-agnostic down to local models**, with a quality-first tier router
- **Tool-direct scheduled actions** that cost zero LLM calls
- **Training pipeline**: your agents' own work becomes fine-tuning data for local specialist models
- **Three-layer split**: the engine stays a clean `git pull`; everything yours lives in your own private repos

## Docs

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design and operations, end to end |
| [docs/PERMISSIONS.md](docs/PERMISSIONS.md) | Permissions & rights — per-platform setup (Linux/macOS/WSL2), failure catalog, runtime permission watch |
| [SCRIPTS.md](SCRIPTS.md) | Every script and what it's for |
| [docs/TRAINING.md](docs/TRAINING.md) | Collect agent turns and fine-tune a local model (nanoGPT / MLX-LM / hosted / DPO-KTO) |
| [docs/LOCAL_MODELS.md](docs/LOCAL_MODELS.md) | Run agents on local models (Ollama / LM Studio) + the quality-first tier router |
| [docs/CREATE_THEN_OPERATE.md](docs/CREATE_THEN_OPERATE.md) | The core concept: big models build tools once, small/no models run them forever |
| [docs/E2E_EXAMPLES.md](docs/E2E_EXAMPLES.md) | Worked end-to-end examples: an agent builds a tool, you collect its usage, train a specialist (nanoGPT / llm-from-scratch / MLX LoRA), and the specialist operates the tool |
| [skills/README.md](skills/README.md) | The skill YAML format |
| [codex/README.md](codex/README.md) | Organizing business knowledge for agents |

## License

MIT
