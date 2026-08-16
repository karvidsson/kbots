# Agent-Led Install — macOS

**Audience: a coding agent** (Claude Code or similar) that has been asked to
install kbots on a Mac, given this repository's URL. You drive the entire
install; the human only does what you cannot — Discord portal work and
one-time auth approvals — and only when you hand it to them.

This guide produces a running install with **two agents**:

| Agent | Tier | Role |
|-------|------|------|
| `main` | coordinator | Primary user-facing agent |
| `engineer` | privileged | Ops/dev agent — edits code, deploys, restarts the service |

macOS only. For Linux, or for a human installing by hand, use the interactive
wizard instead: `uv run python setup.py` (see README Quickstart).

---

## Conduct rules (read first, follow throughout)

1. **You are the wizard.** Work one phase at a time. Verify every checkpoint
   before moving on — never continue past a red checkpoint.
2. **Never ask for, accept, or read a bot token.** Tokens enter the system
   only through `vault-manage.py`, which the human runs in their own
   terminal. If a token ever appears in the chat, tell the human to reset it
   in the Discord portal and store the new one properly.
3. **Use the repo's own APIs** — `scaffold_agent` / `write_identity`
   (`src/core/agent_scaffold.py`), `FernetVault` (`src/vault/fernet.py`),
   and the templates in `config/templates/`. Do not hand-write identity
   files, `.mcp.json`, or `.claude/settings.json`; the scaffolder produces
   them correctly and stays current as the repo evolves.
4. **Echo back every ID** the human gives you (guild, user, channel) before
   using it.
5. **On fatal failure**: stop, list exactly what you created so far, and
   offer the rollback in the appendix.
6. Default install paths (keep unless the human asks otherwise):
   engine `~/kbots`, overlay `~/kbots-overlay`.

---

## Phase 0 — Preflight (you)

Check, and fix what you can yourself:

```bash
sw_vers -productVersion            # macOS
python3 --version                  # need ≥ 3.12
git --version
which uv || curl -LsSf https://astral.sh/uv/install.sh | sh
which claude                       # Claude Code CLI
claude auth status
```

- Python < 3.12 → have the human install it (e.g. `brew install python@3.12`)
  before continuing.
- `claude` missing → install it (`npm install -g @anthropic-ai/claude-code`
  or the human's preferred method).
- `claude auth status` not authenticated → **HUMAN HANDOFF**: have them run
  `claude` (subscription login, browser OAuth) or
  `claude auth login --console` (API billing) in their terminal and confirm.
  The service later runs as this same user, so this login is what the agents
  will use.

Then ask the human up front (offer these defaults):

- Main agent name/display name — default `main` / `MAIN`
- Engineer agent name/display name — default `engineer` / `Engineer`
- Models — default main: `sonnet`, engineer: `opus`
- Their timezone (for `team.json`)

**Checkpoint:** all commands above succeed and `claude auth status` reports
authenticated.

## Phase 1 — Engine + overlay (you)

```bash
git clone <REPO_URL> ~/kbots
cd ~/kbots
scripts/sync.sh                    # installs deps — NEVER use bare `uv sync` in rituals later
cp hooks/pre-commit .git/hooks/    # repo guard hooks
```

Neutralize the engine remote so nothing ever pushes to the core repo from
this install (mirrors `setup.py::_neutralise_core_remote`):

```bash
git -C ~/kbots remote rename origin upstream
git -C ~/kbots remote set-url --push upstream no-push
```

Create the overlay (Layer 3 — all deployment-specific state lives here):

```bash
mkdir -p ~/kbots-overlay/{config,agents,systemd,tools,skills,data,tmp/media,tmp/docs,tmp/scratch}
```

Write `~/kbots-overlay/.gitignore`:

```gitignore
# Runtime data
agents/*/data/
**/MEMORY_CONTEXT.md

# Agent-generated files
tmp/

# Python
__pycache__/
*.pyc

# Claude Code session state
/.claude/

# Secrets
config/secrets.enc
config/secrets.salt
```

Export `KBOTS_OVERLAY` for interactive shells. Detect the human's shell from
`$SHELL`: zsh (macOS default) → `~/.zshrc`; bash → `~/.bash_profile` (what
macOS Terminal login shells read — not `.bashrc`); anything else (fish, …) →
give the human the export line and let them add it. Append:

```bash
export KBOTS_OVERLAY="$HOME/kbots-overlay"
```

> This export only serves interactive tools (`vault-manage.py`, scripts).
> The service never reads shell config — its environment is baked into the
> launchd plist in Phase 5. But without the export, `vault-manage.py` run
> from a fresh shell silently writes a stray vault inside the engine
> checkout, so set it now and have the human open a new terminal (or
> `export` it inline) before Phase 3.

**Checkpoint:** `cd ~/kbots && uv run python -c "import src.main"` exits 0.

## Phase 2 — Vault (you)

Generate a strong passphrase and write the key file the service will unlock
with (there is no TTY under launchd — the key file is mandatory):

```bash
mkdir -p ~/.config
uv run python -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.config/kbots-vault-key
chmod 600 ~/.config/kbots-vault-key
```

Initialize the vault in the overlay:

```bash
cd ~/kbots && KBOTS_OVERLAY=~/kbots-overlay uv run python -c "
from pathlib import Path
from src.vault.fernet import FernetVault
key = Path.home() / '.config/kbots-vault-key'
v = FernetVault(str(Path.home() / 'kbots-overlay/config/secrets.enc'))
v.unlock(key.read_text().strip())
print('vault initialized:', v.list_keys())
"
```

(If the `FernetVault` constructor signature differs in the current code,
read `src/vault/fernet.py` and `vault-manage.py` and match what they do —
those two are the source of truth.)

Tell the human: `~/.config/kbots-vault-key` IS the secret that unlocks
everything — it stays on this machine, mode 600, and is never committed or
shared. `config/secrets.salt` next to the vault must never be regenerated on
a live install — doing so orphans every stored secret.

**Checkpoint:** `~/kbots-overlay/config/secrets.enc` and `secrets.salt`
exist; the key file is mode `-rw-------`.

## Phase 3 — Discord (HUMAN HANDOFF — walk them through it)

kbots policy is **one Discord application per agent** (`create_agent`
refuses to share a bot), so the human creates **two** applications. Give
them these steps verbatim, one application at a time — first for MAIN, then
for Engineer:

1. Open <https://discord.com/developers/applications> → **New Application**
   → name it after the agent (e.g. `MAIN`, `Engineer`).
2. **Bot** tab → under *Privileged Gateway Intents* enable **both**:
   - **Message Content Intent** — without it the bot connects and silently
     ignores every message (the #1 install failure)
   - **Server Members Intent**
   Save.
3. **Bot** tab → **Reset Token** → copy it and *hold onto it* — do NOT
   paste it into this chat. You'll store it in the vault in a minute.
4. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`.
   Bot permissions: View Channels, Send Messages, Read Message History,
   Add Reactions, Attach Files, Embed Links — and for **MAIN only**, also
   **Manage Channels** (it maintains a platform-updates/schedule board).
   Open the generated URL and invite the bot to your server.
5. In Discord: Settings → Advanced → enable **Developer Mode**. Then
   right-click and **Copy ID** for: your **server (guild)**, **yourself**
   (your user), and the **channel** you want HITL approval requests posted
   in. Paste those three IDs here in chat — they are not secrets.

Then have them store both tokens (their terminal, not chat):

```bash
cd ~/kbots && export KBOTS_OVERLAY=~/kbots-overlay
uv run python vault-manage.py
# → store key "discord-token"     = the MAIN bot token
# → store key "discord-engineer"  = the Engineer bot token (use Custom key)
```

**Checkpoint (you):** verify both keys exist and both tokens are live —
without ever printing them:

```bash
cd ~/kbots && KBOTS_OVERLAY=~/kbots-overlay uv run python -c "
from pathlib import Path
import urllib.request
from src.vault.fernet import FernetVault
v = FernetVault(str(Path.home() / 'kbots-overlay/config/secrets.enc'))
v.unlock((Path.home() / '.config/kbots-vault-key').read_text().strip())
for key in ('discord-token', 'discord-engineer'):
    tok = v.get(key)
    assert tok, f'missing vault key: {key}'
    req = urllib.request.Request('https://discord.com/api/v10/users/@me',
                                 headers={'Authorization': f'Bot {tok}'})
    print(key, '->', urllib.request.urlopen(req).status)
"
```

Both must print `200`. A `401` means the token was mis-pasted — have the
human reset that bot's token in the portal and store it again.

## Phase 4 — Config + agents (you)

Write `~/kbots-overlay/config/config.yaml` (substitute the three IDs from
Phase 3):

```yaml
kbots:
  name: kbots
  log_level: info
  data_dir: ./data
connectors:
  discord:
    enabled: true
    guild_id: "<GUILD_ID>"
    accounts:
      main:
        token_key: discord-token
      engineer:
        token_key: discord-engineer
  http:
    enabled: false
    port: 8080
defaults:
  llm:
    provider: claude_code
    model: sonnet
    max_tokens: 4096
  session:
    max_history: 50
    summarize_after: 30
  memory:
    backend: sqlite
    semantic_search: false
    decay_enabled: false
    max_results: 10
security:
  hitl:
    connector: discord
    enabled: true
    channel: "<APPROVALS_CHANNEL_ID>"
    approvers: ["<OWNER_USER_ID>"]
    timeout: 1800
    fail_mode: closed
    poll_interval: 3
    gated_tools: [send_email, install_mcp, create_agent, create_tool, promote_tool, create_trigger, delete_trigger]
  rate_limits:
    mode: log
    defaults:
      max_per_hour: 100
admin_users:
  discord: ["<OWNER_USER_ID>"]
```

(`config/config.yaml.example` is the commented superset if you need a
reference for optional blocks.)

Write `~/kbots-overlay/config/team.json` — copy the shape from
`config/team.json.example`: a `humans` list with one owner object (id, name,
`type: human`, `access: owner`, role, `contact.discord` = the owner user ID,
`preferences.timezone` from Phase 0) and an empty `agents` list.

Scaffold both agents using the repo's scaffolder (this writes the
`agents.yaml` entries AND each agent's `AGENTS.md`, `CLAUDE.md` stub,
`.mcp.json`, and `.claude/settings.json`):

```bash
cd ~/kbots && KBOTS_OVERLAY=~/kbots-overlay uv run python -c "
from pathlib import Path
from src.core.agent_scaffold import scaffold_agent
overlay = Path.home() / 'kbots-overlay'
engine = Path.home() / 'kbots'

# 1. MAIN — coordinator tier, routed to the 'main' bot account
for p in scaffold_agent(
        overlay, 'main', 'MAIN', 'Primary agent',
        model='sonnet', tier='coordinator',
        routing={'discord': {'account': 'main', 'channels': [], 'mentions': True}},
        bot_account='main', engine_root=engine):
    print('wrote', p)

# 2. ENGINEER — privileged ops agent, macOS main-instance pattern,
#    identity from the platform template (same path setup.py takes)
agent_dir = overlay / 'agents' / 'engineer'
tmpl = (engine / 'config/templates/ops-claude-macos.md').read_text()
identity = tmpl.format(display_name='Engineer', agent_dir=agent_dir, engine_root=engine)
for p in scaffold_agent(
        overlay, 'engineer', 'Engineer',
        'Privileged ops agent — code edits, deploys, service restarts. Owner-only.',
        model='opus', tier='privileged',
        routing={'discord': {'account': 'engineer', 'channels': [], 'mentions': True}},
        bot_account='engineer', engine_root=engine,
        claude_md=identity):
    print('wrote', p)
"
```

Adjust names/models to what the human chose in Phase 0. If `scaffold_agent`
rejects an argument, read its signature in `src/core/agent_scaffold.py` and
adapt — the scaffolder is the source of truth, not this doc.

**Checkpoint:** for each of `~/kbots-overlay/agents/{main,engineer}/`:
`AGENTS.md` (real identity), `CLAUDE.md` (a stub beginning `@AGENTS.md`),
`.mcp.json` (env includes `KBOTS_BOT_ACCOUNT` and `KBOTS_AGENT_ID` — these
are load-bearing; without them the agent's Discord tools act as the wrong
bot), and `.claude/settings.json` (missing ⇒ the agent runs with a
restricted tool set). Plus `~/kbots-overlay/config/agents.yaml` containing
both entries.

## Phase 5 — Service (you)

Render the launchd unit from the template `config/kbots.launchd.plist`
(replace the placeholder tokens exactly as `setup.py::step_launchd` does):

| Token | Value |
|-------|-------|
| `__UV__` | `which uv` (e.g. `/opt/homebrew/bin/uv`) |
| `__ENGINE_ROOT__` | `~/kbots` (absolute) |
| `__PATH__` | `$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin` |
| `__HOME__` | the human's home dir |
| `__OVERLAY__` | `~/kbots-overlay` (absolute) |
| `<!-- __EXTRA_ENV__ -->` | remove the line (no Layer-2 modules on a fresh install) |

Write the result to `~/kbots-overlay/systemd/com.kbots.agent.plist`, then:

```bash
cp ~/kbots-overlay/systemd/com.kbots.agent.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kbots.agent.plist
```

Optional but recommended — the auto-recovery watchdog:

```bash
cd ~/kbots && KBOTS_OVERLAY=~/kbots-overlay bash scripts/install-watchdog.sh
```

## Phase 6 — Verify (you, then human)

Poll the logs for both bots coming online (budget ~60s):

```bash
tail -n 100 ~/kbots-overlay/data/launchd.stderr.log | grep "ready as"
# expect one line per bot, e.g.:
#   Discord bot 'main' ready as MAIN#1234 (guilds: 1)
#   Discord bot 'engineer' ready as Engineer#5678 (guilds: 1)
```

Also confirm the heartbeat is fresh: `~/kbots/data/heartbeat` should contain
a unix timestamp updated within the last minute or so.

Then **HUMAN HANDOFF**: in Discord, send `@MAIN hello` and confirm a reply;
then `@Engineer hello`. First replies can take 20–60s (cold CLI session).

If a bot connects but never replies, check in this order:

| Symptom | Cause |
|---------|-------|
| `ready as` logged, no reply ever | Message Content Intent not enabled → portal, Bot tab |
| Replies in some channels only | Bot's role can't view the channel |
| Preflight warning `missing Discord token` | Vault key name ≠ `token_key` in config.yaml |
| Bot online but "connected-but-unserved" warning in logs | No agent routes to that account in agents.yaml |
| Every tool denied for an agent | `.claude/settings.json` missing, or `~/.claude.json` root-owned |

## Phase 7 — Summary (you)

Report to the human:

- What was created: `~/kbots` (engine), `~/kbots-overlay` (their deployment
  — config, agents, data; the only dir that is *theirs*), vault key file,
  LaunchAgent `com.kbots.agent`.
- **Update ritual**: `cd ~/kbots && scripts/update.sh` — never a bare
  `git pull` + restart; the service starts with `uv run --no-sync`, so
  skipping the dependency sync crashes on new deps or silently runs stale
  code.
- **More agents**: ask MAIN in Discord ("create an agent called research
  that…") — it scaffolds via `create_agent` with HITL approval; restart via
  `/admin reboot`.
- Docs: `README.md`, `ARCHITECTURE.md`, `docs/PERMISSIONS.md`,
  `scripts/settings.py` for post-install configuration.

---

## Appendix — Rollback

To fully remove a failed or unwanted install:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.kbots.agent.plist 2>/dev/null
rm -f ~/Library/LaunchAgents/com.kbots.agent.plist
rm -f ~/Library/LaunchAgents/com.kbots.watchdog.plist   # if watchdog installed
rm -rf ~/kbots ~/kbots-overlay
rm -f ~/.config/kbots-vault-key
# and remove the KBOTS_OVERLAY export from the shell profile
```

Warn the human before deleting `~/kbots-overlay` — it contains their vault
and all agent data. Never delete or regenerate `config/secrets.salt` on an
install you intend to keep.
