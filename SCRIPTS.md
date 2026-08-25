# kbots Scripts Reference

## Setup & Operations

### `setup.py` — Interactive Setup Wizard
Takes a fresh clone all the way to a running agent; needed only once, right after cloning.
```bash
uv run python setup.py
```

**Steps:**
1. **Dependencies** — verifies Python, uv, jq, and the Claude Code CLI are present; confirms Claude auth (`claude auth status`); runs `scripts/sync.sh`
2. **Install location** — clones the engine to a dedicated install dir (`/opt/kbots` Linux, `~/kbots` macOS) so your working checkout stays disconnected from the running service
3. **Overlay** — creates deployment overlay directory structure; exports env vars to the right shell profile (zsh/bash)
4. **Git hooks** — installs the pre-commit hook, refreshing it on re-runs if it changed
5. **Modules** — finds Layer 2 extension modules and asks which ones to enable
6. **Vault** — sets up the encrypted credential vault, reusing one if it already exists
7. **Main agent & Discord bot** — one name ("Atlas") drives the display name, folder, config key and bot account; then token, guild, install link, model, personality and routing — one agent, one bot, one step
8. **Team** — configures the owner and team members
9. **Machine control** — how much of the machine the main agent may touch: none (default), user shell, or root via a sudoers rule
10. **HITL** — approval channel and approvers; which tools are gated (`send_email`, `install_mcp`, `create_agent`)
11. **Compression** — context compression, if wanted; plus optional sub-steps for **Local models** (Ollama / LM Studio + tier router) and **Training-data collection**
12. **Optional features** — Python extras (data analysis, reports, web search, …) saved to `<overlay>/extras` so every future sync keeps them installed
13. **Config generation** — produces config.yaml, agents.yaml, team.json, mcp.yaml, and the agent AGENTS.md
14. **Ops instance** — optionally adds a privileged dev/ops agent (one name here too — bot account and folder derive from it) with its own bot and service
15. **Extras** — additional team members or bots (further *agents* are created post-setup by your main agent via its `create_agent` tool)
16. **Service** — Linux: systemd units via `scripts/install-systemd.sh`; macOS: launchd plist. Starts the service and verifies the agent comes online
17. **Browser** — optionally fetches Chromium for headless browsing

### `uninstall.py` — Uninstall Wizard
Reverses `setup.py`: detects the install (overlay, engine clone, service, sudo rule, shell env, vault key), **offers to export it first**, then removes everything after confirmation. Leaves the engine source checkout in place.
```bash
uv run python uninstall.py                # export (optional) + remove
uv run python uninstall.py --export-only  # just bundle it, remove nothing
```

### `scripts/migrate.py` — Export / Import a Deployment
Move an installation between machines. `export` bundles the overlay (config, vault, agents, memory) into a portable tarball; `import` restores it and rewrites the machine-specific absolute paths (engine root, overlay, home). Regenerable files (embedding model, tmp, logs) are excluded; the memory DB is kept.
```bash
uv run python scripts/migrate.py export --overlay <overlay> --out ~ [--with-key]
uv run python scripts/migrate.py import <bundle.tar.gz> --overlay <dest> --engine <engine-root>
```

### `scripts/dev.py` — Dev Harness (ephemeral test agent)
Spin up a disposable test agent in an isolated instance, chat with it in the terminal, and tear everything down on exit. Uses a `.dev/` workspace (gitignored), the `dev` profile (own lock + data dir — a live install is untouched), the `repl` terminal connector, and real Claude by default (`--mock` for the offline echo provider).
```bash
uv run python scripts/dev.py chat [--mock] [--keep] [--agent-prompt "..."]
uv run python scripts/dev.py down      # force cleanup
uv run python scripts/dev.py test      # pytest
uv run python scripts/dev.py lint      # ruff
uv run python scripts/dev.py check     # lint + tests
```

### `scripts/full-control.sh` — Grant/Revoke Root Control
Installs or removes a `sudoers.d` rule giving the kbots service user passwordless sudo — the "root" level of full machine control. Used by the setup wizard (Step 10) and settings.py, or run directly.
```bash
scripts/full-control.sh grant [user]   # passwordless sudo (asks your password once)
scripts/full-control.sh revoke         # remove it
scripts/full-control.sh status         # show current state
```

### `scripts/update.sh` — One-Command Update
Updates a running install: pulls, syncs deps across layers, then hot-reloads (tools/skills/codex-only changes) or restarts the service (core changes — systemd on Linux, launchd on macOS). Also available from Discord as `/admin update`.
```bash
cd "$KBOTS_HOME" && scripts/update.sh
```

### `scripts/self-deploy.sh` — Safe Deploy (test-gated, auto-rollback)
Like `update.sh` but with safety rails, for letting an agent (or you) ship changes to the live service. Pulls, syncs, then **gates on `ruff` + full `pytest`** before restarting; after restart it **health-checks the boot** and **auto-rolls-back to the previous commit** if tests fail or the service doesn't come up cleanly. Deterministic — the safety is in the script, not an agent's judgement. The box is never left on broken code.
```bash
cd "$KBOTS_HOME" && scripts/self-deploy.sh
```

### `scripts/install-watchdog.sh` — Automatic-Recovery Watchdog (lifeboat)
Installs an independent watchdog service (its own launchd job / systemd timer, runs every 2 min) that watches the engine's heartbeat. If the main service crash-loops, hangs, or a bad deploy takes it down, the watchdog **automatically rolls the install back to the last known-good commit and restarts** — no human, no second bot. The watchdog script lives in the overlay (outside the volatile checkout) so a broken checkout can't disable its own recovery. `self-deploy.sh` and the watchdog both record/read `data/last-good-commit`.
```bash
scripts/install-watchdog.sh            # install + start
scripts/install-watchdog.sh uninstall  # remove
```

### `setup.sh` — Deprecated Stub
Forwards to `setup.py` and prints a deprecation notice; retained for backwards compatibility.
```bash
./setup.sh  # equivalent to: uv run python setup.py
```

### `scripts/avatar.py` — Agent Avatar Generator

```bash
uv run python scripts/avatar.py --eyes capsule --accent red --out agents/foo/avatar
uv run python scripts/avatar.py --list
uv run python scripts/avatar.py --eyes wink --accent blue --out agents/foo/avatar --set-discord foo
```

Composes an agent avatar from the brand template (eye style = expression,
accent color = identity; frame/screen fixed so the fleet reads as one family).
Writes `<out>.svg` always and a Discord-ready 512px `<out>.png` when
Playwright Chromium is available. Offered automatically when creating an
agent via `settings.py`. `--set-discord <account>` also uploads the PNG as
that bot account's Discord avatar via the API (token from the vault,
key-file unlock; the settings.py flow can prompt for the passphrase).
Note: Discord rate-limits avatar changes to a couple per half hour.

### `scripts/settings.py` — Interactive Settings Manager
Terminal UI for inspecting and changing any configuration after install — no need to run setup again.
```bash
uv run python scripts/settings.py
```

**Menu options:**
1. **LLM defaults** — timeout, max tokens, provider, and model
2. **Connectors** — manage Discord bot accounts, toggle platforms on or off
3. **Session** — summarize threshold and max history
4. **Memory** — backend choice, semantic search on/off, decay settings, result cap
5. **HITL** — approval channel and timeout, manage approvers, gated tools (add or remove, with the available tools listed)
6. **Rate limits** — enforce vs. log-only mode, limits per tool, wildcard patterns
7. **Compression** — on/off plus level
8. **Admin users** — which Discord user IDs get admin access
9. **Identity** — data directory, log level, system name
10. **Agents** — see every agent; adjust a given agent's model or routing
11. **Create agent** — spins up a new agent: pick a tier, assign a bot, filter channels
12. **Timers** — systemd timers with status and schedule; enable, disable, or reschedule them
13. **Ops instance** — set up the privileged dual-instance arrangement
14. **Vault secrets** — list, add, update, or delete vault entries

### `vault-manage.py` — Interactive Vault Manager
Add, update, delete, list, and check encrypted secrets.
```bash
uv run python vault-manage.py
```

### `scripts/chrome-debug.sh` — Debug Chrome for the `chrome_browser` Tool (macOS)
Launches a separate Google Chrome with remote debugging enabled so the `chrome_browser` tool can drive your **real** browser — genuine fingerprint and logged-in sessions — to reach sites that block the headless `browser` tool. It runs on a debug profile seeded from your real one (cookies/logins carry over), leaving your everyday Chrome untouched. A dedicated profile dir is required because Chrome 136+ refuses remote debugging on the default profile for security.
```bash
scripts/chrome-debug.sh              # start (or reuse) the debug Chrome
scripts/chrome-debug.sh --refresh    # re-seed logins from your live profile
scripts/chrome-debug.sh --status     # is the debug port up?
scripts/chrome-debug.sh --install    # supervise under launchd (recommended)
scripts/chrome-debug.sh --uninstall  # remove the launchd job
```
The `chrome_browser` tool auto-runs this on first use, so you normally don't call it directly. Env: `KBOTS_CHROME_DEBUG_PORT` (default 9222), `KBOTS_CHROME_DEBUG_DIR` (default `~/.kbots-chrome-debug`).

**Supervision (`--install`):** an ad-hoc debug Chrome dies silently on a crash, a Chrome self-update relaunch (which drops CLI flags), ⌘Q, or a reboot — and CDP with it. `--install` writes a `com.kbots.chrome-debug` LaunchAgent with `KeepAlive`, so launchd restarts it with the right flags every time. On every successful start the script writes an endpoint discovery file (`$KBOTS_OVERLAY/data/chrome-debug.json`: port, user-data-dir, pid) which the `chrome_browser` tool uses to verify the responder on the port is *our* Chrome — a squatted port or the user's own flag-ignoring Chrome now gets a clear refusal instead of a confusing session. Note your own everyday Chrome **cannot** expose CDP (Chrome ≥136 ignores the flag on the default profile); logins the agents need belong in the debug Chrome's named profiles via the sign-in-once flow.

**Dedicated per-agent instances:** by default every agent shares one debug Chrome (identity separation via named profiles, but one CDP port — any attached agent can reach any profile's tabs, and driving is serialized). For an agent holding real credentials, give it its own Chrome in `agents.yaml`:
```yaml
  atlas:
    chrome_instance: {port: 9223}          # dir defaults to ~/.kbots-chrome-<agent>
```
That agent then gets its own data dir (real cookie isolation), its own reservation lane (parallel driving), and its own endpoint file. Supervise it too: `KBOTS_CHROME_DEBUG_PORT=9223 KBOTS_CHROME_DEBUG_DIR=~/.kbots-chrome-atlas scripts/chrome-debug.sh --install` (the label becomes `com.kbots.chrome-debug-9223`). Cost: one Chrome's RAM per dedicated agent.

## Testing

### `scripts/test-tools.py` — E2E Tool Test Suite
Exercises tools against real APIs and prints pass/fail results.
```bash
# Run all tests
uv run python scripts/test-tools.py

# Test a specific tool
uv run python scripts/test-tools.py --tool team_list

# Filter by category keyword
uv run python scripts/test-tools.py --category google
uv run python scripts/test-tools.py --category trello
```

### `scripts/eval_skill.py` — Skill Eval Harness
Scores a skill's tool-calling accuracy on held-out fixtures — one round per
fixture, the proposed call is checked but **never executed** (side-effect-free).
Trap fixtures (`expect_no_tool`) catch over-eager calling. Used to measure a
trained specialist before routing it in (see docs/E2E_EXAMPLES.md).
```bash
# fixtures.jsonl:
#   {"input": "movie time", "expect_tool": "set_scene", "expect_args": {"scene": "movie"}}
#   {"input": "what scenes are there?", "expect_no_tool": true}
uv run python scripts/eval_skill.py --skill scene_specialist --fixtures fixtures.jsonl
# pin provider/model, fail CI below a threshold:
uv run python scripts/eval_skill.py --skill scene_specialist --fixtures fixtures.jsonl \
    --provider local --model scene-specialist --min-pass 0.8
```

## Monitoring Scripts

Every monitoring script goes through `scripts/lib-alert.sh` to post Discord alerts (credentials from the Fernet vault). `scripts/install-timers.sh` installs them as systemd timers.

### `scripts/health-audit.sh` — Full System Audit
Audits the whole system — services, timers, resources, network, security, memory system, vault, MCP servers, tools/skills, databases, application health, logs, git state — with `health-config.yaml` controlling what runs.
```bash
# Timer mode (default) — alerts on issues, heartbeat on success
scripts/health-audit.sh

# Report mode — full PASS/WARN/FAIL output (used by /system-audit slash command)
scripts/health-audit.sh --report
```
A timer fires it every 12 hours; the `/system-audit` Discord slash command triggers it on demand (executed directly, without an LLM round-trip).

### `scripts/monitor-integrity.sh` — File Integrity Monitor
Compares SHA256 checksums of critical files to a stored baseline every 12 hours and alerts when anything differs.
```bash
# Update baseline after intentional changes
scripts/monitor-integrity.sh --update-baseline
```

### `scripts/regen-memory-context.sh` — Memory Context Generator
Rebuilds the memory statistics and the service-health snapshot on a 30-minute cycle.

### `scripts/memory-decay.sh` — Memory Lifecycle
Daily job that decays memory confidence, moves old memories into the archive, and deletes archives past their expiry.

### `scripts/lib-alert.sh` — Shared Alert Helper
Shared helper every script sources; its `send_alert()` posts to Discord with the bot token pulled from the Fernet vault.
```bash
source scripts/lib-alert.sh
send_alert "Something happened"
```

### `scripts/lib-alert.sh` — `send_report()` Function
`lib-alert.sh` also exposes `send_report()`, which posts a formatted report to Discord and can attach a file:
```bash
source scripts/lib-alert.sh
send_report "Report title" "$report_content" "/path/to/attachment.txt"
```
Takes a title string, a content string, and optionally a file path whose file is attached to the Discord message.

### `scripts/install-timers.sh` — Timer Installer (legacy)
Installs every systemd timer and service file found in `config/timers/`.
```bash
sudo scripts/install-timers.sh
```
When the `KBOTS_OVERLAY` env var is set, its value gets baked into the rendered service files. Run the script again after a Core update or overlay change so the units regenerate with current paths.

### `scripts/install-systemd.sh` — Systemd Unit Installer
Links the generated units — timers plus the service — into `/etc/systemd/system/`, does a daemon-reload, and enables the timers. `setup.py` invokes it for you.
```bash
# Called by setup.py, or run manually:
sudo bash scripts/install-systemd.sh <overlay-dir> [--enable-service]
```

### `scripts/vendor-mermaid.sh` — Vendor mermaid.js for Offline Diagrams

Fetches a pinned `mermaid.min.js` (`MERMAID_VERSION`, ≥ 11.16 for `wardley-beta`/`swimlane-beta`) into `src/lib/vendor/` so `render_diagram` and the process-mapping tools draw diagrams without the CDN. `scripts/sync.sh` runs it best-effort on every deploy; when the file is missing, rendering falls back to jsDelivr. `--force` re-downloads.

```bash
scripts/vendor-mermaid.sh            # fetch if missing / outdated
MERMAID_VERSION=11.16.1 scripts/vendor-mermaid.sh --force
```

### `scripts/compress-context.sh` — Context Compression
Compresses agent context files in bulk — codex docs, skill prompts — to cut input tokens. Safe to re-run: files that haven't changed are skipped, and agent identity files (AGENTS.md) are left out by default.
```bash
# Dry run — show savings without writing
scripts/compress-context.sh --dry-run

# Compress with specific level
scripts/compress-context.sh --level lite      # filler phrases only
scripts/compress-context.sh --level standard  # filler + contractions (default)
```

### `scripts/google-reauth.py` — Google OAuth2 Re-auth
Re-authenticates Google OAuth2 credentials once their tokens have expired.
```bash
uv run python scripts/google-reauth.py
```

## Running kbots

### Production (systemd)
```bash
systemctl start kbots.service
systemctl stop kbots.service
systemctl status kbots.service
journalctl -u kbots -f
```

### Development
```bash
uv run python -m src.main
```

### Test Profile
```bash
uv run python -m src.main --profile test
```
