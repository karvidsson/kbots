# kbots

## What Is This?

kbots bridges messaging platforms (Discord today; others planned) to long-lived LLM sessions, each anchored in a project directory. The system is self-contained and tied to no particular LLM provider.

## Design Philosophy

- **No infrastructure over feature-sprawl** — features are drop-in modules; the moving parts stay one process + SQLite
- **Zero maintenance** — everything persists in SQLite; no external database, no Docker, no cron jobs to babysit
- **LLM agnostic** — Claude API, Groq, Ollama, or anything speaking the OpenAI protocol
- **Project-scoped sessions** — every agent lives in its own project directory and sees its files as context
- **Easy to extend** — a new tool, API, or skill is one decorated Python function
- **Simple security** — credentials sit in a Fernet-encrypted vault; no proxy chains or containers required
- **Inter-agent communication** — a lightweight message bus lets agents talk to each other

## Architecture — Everything Is a Module

One asynchronous Python process; every capability is a pluggable module the registry finds on its own.
Put a file in the matching folder, point config at it, and it works.

```
Registry (auto-discovers all modules)
    │
    ├── Connectors   src/connectors/    discord, telegram, slack, http...
    ├── LLM          src/llm/           claude, groq, ollama, openai...
    ├── Memory       src/memory/        sqlite, postgres, redis...
    ├── Vault        src/vault/         fernet (default)
    ├── Tools        src/tools/         @tool decorated functions
    ├── Skills       skills/            YAML (prompt + tool list)
    ├── MCP          config/mcp.yaml    auto-surfaces as native tools
    └── APIs         src/apis/          inbound webhooks
```

An agent = a project folder plus a config block that selects modules. [ARCHITECTURE.md](ARCHITECTURE.md) covers the complete design.

## Key Directories

```
src/
  core/          — registry, agent manager, router, middleware, base interfaces
  connectors/    — one file per platform (discord.py, telegram.py, etc.)
  llm/           — one file per provider (claude_code.py, etc.)
  memory/        — one file per backend (sqlite.py, etc.)
  vault/         — credential encryption (fernet.py)
  tools/         — @tool decorated functions, auto-discovered
  lib/           — shared libraries (compressor.py)
  auth/          — OAuth2 token refresh, etc.
  apis/          — reserved for inbound API modules (webhooks: src/connectors/webhook.py)
config/          — YAML: config.yaml, agents.yaml, mcp.yaml
agents/          — one folder per agent (SOUL.md + workspace + local data)
skills/          — YAML skill definitions (prompt + tool list)
extras/          — opt-in integrations, NOT auto-discovered (see extras/README.md)
data/            — SQLite databases, audit logs, state (gitignored)
```

## Tech Stack

- **Python 3.12+** with asyncio
- **SQLite** (WAL mode) as the sole persistence layer
- **Claude Code CLI** driving the primary LLM engine (Pro/Max subscription, or API/Console billing)
- Alternative LLM providers via a direct HTTP client — any OpenAI-compatible endpoint; Ollama and LM Studio are auto-detected
- **discord.py** for Discord
- *python-telegram-bot* for Telegram (roadmap)
- **cryptography** (Fernet) powering the credential vault
- *FastAPI* for HTTP API / webhooks (roadmap)

## Development

```bash
scripts/sync.sh            # install deps (all layers)
uv run python -m src.main  # run locally
```

## Deployment

To update a live install (`git pull` + restart), use the procedure in [ARCHITECTURE.md → Operations → Updating a Running Install](ARCHITECTURE.md#updating-a-running-install). Do **not** just `git pull` and `systemctl restart`: the service units launch with `uv run --no-sync`, so `sudo -u kbots scripts/sync.sh` has to run between the pull and the restart — skip it and the service either crashes on a missing dep or quietly keeps executing stale code.

## File Ownership

Everything under the install directory belongs to the service user, `kbots:kbots`. Because the main service and its timers execute as `kbots`, a root-owned file inside the tree is a time bomb: the service can read it but never write or delete it, and one root-owned file in `.venv/` is enough to break `uv sync` / `uv run`.

**Editing repo files as root** (e.g. from a root Claude Code session)? Run `chown kbots:kbots <file>` after each save — most editors, the `Edit`/`Write` tools included, write a fresh file that takes on the editing user's ownership instead of keeping the original's. Once a batch of edits is done, check with:

```bash
find "$KBOTS_HOME" -not -user kbots 2>/dev/null
```

No output means the tree is clean; anything listed needs `sudo chown -R kbots:kbots <paths>`.

## Three-Layer Architecture

This repository holds **Layer 1 (Core)**: the open-source engine that every deployment loads.

```
Layer 1: karvidsson/kbots      (this repo, public)  — Core engine + generic tools
Layer 2: (private repo)                           — Domain-specific tools/skills
Layer 3: (private repo)                           — Per-deployment config, agents, data
```

Two env vars tell the engine where Layers 2 and 3 live:
- `KBOTS_MODULES` — Layer 2 module directories, colon-separated
- `KBOTS_OVERLAY` — location of the Layer 3 overlay directory

### What belongs in Core (this repo)

- Framework code: `src/core/`, `src/connectors/`, `src/llm/`, `src/memory/`, `src/vault/`, `src/auth/`
- Engine-API tools (the agent's interface to kbots itself) and generic primitives: memory, team, web, browser, discord, audio, video, tmux, android, ingest, compress
- `extras/` — curated opt-in integrations (google, trello, notion, github, cloudflare, gemini, stocks, news, monitoring, shelly). **Not auto-discovered**; installed per deployment by copying into the overlay. Vendor integrations belong here, not in `src/tools/`
- The setup wizard, MCP server, generic skills, scripts, and timers
- Only example configs (`config/*.example`, `agents/rescue/CLAUDE.md.example`)

### What does NOT belong in Core

- Identities of client agents — CLAUDE.md files belonging to specific named agents
- Live config files — config.yaml, agents.yaml, or a team.json holding real data
- Tools tied to a specific domain (Layer 2 material)
- Anything identifying: personal names, Discord IDs, API keys
- Skills, timers, scripts, or ETL pipelines built for one client

### Development workflow

A pre-commit hook rejects commits made directly on `main`; every change travels through a feature branch:

```bash
git checkout -b fix/description        # create branch
# make changes, test with: sudo systemctl restart kbots
git add <files>
git commit -m "fix: description"
git push --no-verify -u origin fix/description
gh pr create                           # PR for cross-review
# other install reviews and approves
# merge via GitHub, then both installs: git checkout main && git pull
```

### Commit rules

- Commit messages follow **Conventional Commits**: `type(scope): summary` — types are
  `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`, `ci`. Scope is optional;
  summary is imperative and lowercase (e.g. `feat(discord): thread replies in approvals`)
- Every change lands through a **cross-reviewed PR** approved by a maintainer
- Never use `--no-verify` to sidestep the pre-commit hook guarding `main`
- Check the diff for personal names, company names, and numeric IDs before every commit
- Force-pushing `main` needs explicit approval — otherwise don't

## Key Conventions

- **Modules are files** — a .py placed in the correct folder is live immediately; nothing to import, nothing to register.
- **Agents are config** — one YAML block naming an LLM, a memory backend, tools, skills, and routing defines an agent.
- **Tools are decorated functions** — put `@tool` on an async function; its type hints become the schema.
- **Skills are YAML** — just a prompt plus a tool list, zero code.
- **MCP auto-surfaces** — tools from a connected MCP server show up as native ones; the LLM can't tell them apart.
- **Secrets in vault** — never in env vars or config files; encrypted at rest, unlocked by a passphrase at startup.
- **No ORMs** — plain SQL plus small helper functions
- **Type hints everywhere**; keep abstractions to a minimum

## Docs

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — Complete architecture and operations reference
- **[SCRIPTS.md](SCRIPTS.md)** — Every script and how to run it
- **[skills/README.md](skills/README.md)** — The skill file format
- **[docs/PERMISSIONS.md](docs/PERMISSIONS.md)** — Permissions & rights: per-platform setup, failure catalog, permission watch
