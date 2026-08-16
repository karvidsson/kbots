# Permissions & Rights

Most "why won't my agent *do* anything?" problems are rights problems. This guide
covers how agent permissions actually work, how to set them up correctly on each
platform, the failure modes we have hit in production, and the runtime watchdog
that escalates new ones to your main agent.

Three layers catch rights problems:

1. **Boot preflight** — checks ownership and allow-lists at startup, prints the
   exact fix command per problem (watch the boot log for `permissions:` lines).
2. **Permission watch** — detects failures *while the service runs* and briefs
   your main agent, which reports to you with the exact fix and the access it
   needs. See [Runtime detection & escalation](#runtime-detection--escalation).
3. **This document** — for understanding and for anything the first two can't fix
   themselves.

## How agent permissions work

Understanding the chain makes every failure below obvious:

```
service user (runs the engine)
  └─ spawns `claude` CLI per agent, cwd = agent workspace
       ├─ ~/.claude.json            workspace TRUST registry (per service user)
       ├─ <workspace>/.claude/settings.json   the agent's tool allow-list
       └─ --allowedTools / --add-dir          per-spawn grants from agents.yaml
```

- **Trust comes first.** Headless Claude Code **ignores** a workspace's
  `settings.json` allow-list unless the workspace is marked trusted in
  `~/.claude.json` (`hasTrustDialogAccepted`). There is no dialog to accept
  headless, so the engine writes this entry itself on **every spawn**
  (`src/llm/claude_code.py::_ensure_workspace_trusted`). If that write can't
  happen, the allow-list silently stops applying and **every tool is denied**.
- **The allow-list grants tools.** Each agent workspace carries
  `.claude/settings.json` with `permissions.allow` (written by the setup wizard
  and `create_agent`, from the agent's tier). Missing file = restricted agent.
- **Explicit grants per spawn.** The engine always passes `--allowedTools`
  (computed from the agent's tools + MCP servers) and `--add-dir` for each
  `extra_dirs` entry in `agents.yaml`.

One consequence worth internalizing: **everything hangs off files owned by the
service user**. Any process that re-owns those files — most commonly a `claude`
session run as root — breaks the whole chain invisibly.

## Correct setup per platform

| | Linux | macOS | Windows |
|---|---|---|---|
| **Service manager** | systemd (`kbots.service`) | launchd (`kbots.launchd.plist`) | WSL2 → systemd |
| **Service user** | dedicated `kbots` user | your login user | your WSL user |
| **Install dir** | `/opt/kbots` | `~/kbots` | `~/kbots` inside WSL |
| **Claude config** | `~kbots/.claude.json`, `~kbots/.claude/` | `~/.claude.json`, `~/.claude/` | inside WSL home |
| **Ownership rule** | everything `kbots:kbots` | everything your user | everything your WSL user |

### Linux (systemd)

The wizard creates a dedicated `kbots` service user. Two rules keep it healthy:

- Everything under the install dir, the overlay, **and the service user's
  `~/.claude*`** must be owned by `kbots:kbots`.
- Operate on the install only via `sudo -u kbots …` (git pull, sync.sh) — never
  as root directly. Root-owned files in `.venv/` break `uv sync`/`uv run`.

```bash
# Verify (should print nothing):
sudo find "$KBOTS_HOME" ~kbots/.claude ~kbots/.claude.json -not -user kbots 2>/dev/null
# Repair:
sudo chown -R kbots:kbots "$KBOTS_HOME" ~kbots/.claude ~kbots/.claude.json
```

The main service unit is sandboxed (`ProtectSystem=strict`), which itself
prevents most accidental cross-ownership writes.

### macOS (launchd)

The service runs as **your login user** — there is no separate service user, so
your interactive sessions and the service share `~/.claude.json`. That sharing
is the #1 source of breakage: **any `claude` session run under `sudo`/root with
your `$HOME` rewrites `~/.claude.json` root-owned `0600`**, and the service can
no longer read it. See [the root-session hazard](#the-1-failure-root-owned-claude-config).

```bash
# Verify (should print nothing):
find ~/kbots ~/kbots-overlay ~/.claude ~/.claude.json -not -user "$(whoami)" 2>/dev/null
# Repair:
sudo chown -R "$(whoami)" ~/kbots ~/kbots-overlay ~/.claude ~/.claude.json
```

### Windows

Native Windows is **not a supported service target** — the engine, service
units, and every ownership check in this guide assume a POSIX filesystem. Run
kbots inside **WSL2** (Ubuntu recommended) and follow the Linux instructions
verbatim from within WSL. Two Windows-specific caveats:

- Keep the install and all Claude config inside the WSL filesystem
  (`~/kbots`, `~/.claude*`) — **not** under `/mnt/c/…`. NTFS-mounted paths have
  emulated ownership; `chown` there is a no-op and trust/ownership checks
  misbehave.
- If you also use Claude Code natively on the Windows side, it keeps a separate
  `%USERPROFILE%\.claude.json` — the two never conflict as long as the service
  and its agents live entirely in WSL.

## The #1 failure: root-owned Claude config

**Symptom:** an agent that worked fine suddenly answers every request with some
variant of *"Claude requested permissions to …, but you haven't granted it
yet"* — including for files it modified minutes earlier. Often all agents at
once.

**Cause:** `~/.claude.json` (or files under `~/.claude/`) became owned by
another user — almost always because a `claude` session ran as root against the
service user's home. The CLI rewrites `~/.claude.json` via atomic rename, so the
replacement file inherits **root ownership and mode 0600**: the service can no
longer even *read* it, workspace trust can't be verified or written, and every
tool is denied. The engine detects this and logs loudly:

```
ERROR [src.llm.claude_code] Cannot mark workspace trusted (<workspace>): [Errno 13]
Permission denied: '…/.claude.json'. … EVERY TOOL will be denied for this agent
until this is fixed. Fix: sudo chown $(id -un) …/.claude.json
```

**Fix** (30 seconds, no restart needed — trust re-heals on each agent's next turn):

```bash
sudo chown -R <service-user> <service-home>/.claude.json <service-home>/.claude
```

**Prevention:**

- **Never run `claude` as root against a home directory the service uses.**
  This is the whole failure class. On Linux the dedicated `kbots` user isolates
  you; on macOS it's on you.
- If you *must* work in root shells on the service machine (common on macOS),
  either:
  - give root sessions their own config: `export CLAUDE_CONFIG_DIR=/var/root/claude`
    (credentials live in the OS keychain/keyring, not in `.claude.json`, so this
    is safe), or
  - install self-repair hooks in the *interactive* user's `~/.claude/settings.json`
    so any root session fixes its own damage:

    ```jsonc
    {
      "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "/path/to/fix-ownership.sh", "timeout": 15}]}],
        "PostToolUse":  [{"hooks": [{"type": "command", "command": "/path/to/fix-ownership.sh", "timeout": 15, "async": true}]}],
        "Stop":         [{"hooks": [{"type": "command", "command": "/path/to/fix-ownership.sh", "timeout": 15}]}]
      }
    }
    ```

    where `fix-ownership.sh` chowns `~/.claude.json` and anything under
    `~/.claude` back to the service user (a no-op when nothing is wrong):

    ```sh
    #!/bin/sh
    OWNER=<service-user>; HOME_DIR=<service-home>
    chown "$OWNER" "$HOME_DIR/.claude.json" 2>/dev/null
    find "$HOME_DIR/.claude" ! -user "$OWNER" -exec chown "$OWNER" {} + 2>/dev/null
    exit 0
    ```

- The **permission watch sweep** (below) is the backstop for anything that slips
  through — including damage done while every agent is idle.

**Related blast radius** of root sessions on the service machine: `~/.cache/uv`
(breaks `uv sync`), `.git/config` in the install tree (breaks git for the
service user), and anything they touch in the overlay. The sweep's ownership
checks and the repair commands above cover these too.

## Runtime detection & escalation

`src/core/permission_watch.py` watches for rights failures while the service
runs and escalates them to a **main agent** of your choice — so instead of
finding out hours later from a confused agent, you get a Discord message that
says what broke, the exact commands to fix it, and whether SSH is enough.

**What it detects:**

| Trigger | Detection point |
|---|---|
| Claude config unreadable (trust broken) | the moment a spawn hits it |
| Tool denials mid-session (`haven't granted it yet`) | the CLI event stream, per agent |
| Wrong ownership on Claude config / agent workspaces | periodic sweep (default: every 5 min) |

Explicit human rejections (HITL denials, a user saying no to a permission
prompt) are **not** escalated — only the signature that means "the plumbing is
broken".

**Configuration** (Layer 3 `config.yaml`):

```yaml
security:
  permission_watch:
    enabled: true        # default true
    agent: ""            # main agent to brief, e.g. your coordinator agent
    connector: discord
    channel: ""          # channel where the briefing lands and the agent reports
    interval: 300        # seconds between ownership sweeps
    cooldown: 3600       # seconds before the same issue is re-reported
```

**Escalation chain:** configured agent → security alert channel
(`security.alert_channel`) → service log. With no config at all you still get
deduplicated `permission-watch` log errors.

**What the main agent receives** is a structured briefing: the issue, the host,
the affected agent, the exact per-OS fix commands, and one of these access
levels so the owner knows what the fix takes *before* starting:

- **remote shell (SSH) with sudo/admin rights** — ownership fixes, chown/chmod
- **a human in an interactive terminal** — e.g. re-running `claude` login flows
- **a human at the machine's desktop** — GUI-only steps (macOS keychain prompts,
  screen sharing)
- **a web dashboard** — Discord developer-portal settings; no machine access

The agent is instructed to verify the finding first, apply the fix itself when
it safely can, and otherwise report to the owner with the exact steps.

## Agent tool allow-lists — `.claude/settings.json`

Each agent's workspace carries a `.claude/settings.json` with a
`permissions.allow` list. **This is what grants the agent its tools** (Bash,
web, MCP tools, …), scoped by the agent's tier (`privileged` / `coordinator` /
`assistant` — see `src/core/agent_scaffold.py::cc_allow_for_tier`). The setup
wizard and `create_agent` write it automatically; if it's missing, the agent
runs with a restricted tool set. The preflight flags a missing allow-list per
agent, and existing files are never overwritten — edit them freely.

## Discord bot permissions & intents

Set these in the [Discord Developer Portal](https://discord.com/developers/applications)
(per bot) and when inviting the bot to the server:

- **Privileged Gateway Intents → enable *Message Content Intent*.** Without it,
  agents cannot read message text at all.
- **Bot permissions** (invite scope): *View Channels, Send Messages, Read
  Message History, Add Reactions, Attach Files*. Add **Manage Channels** if you
  want the main agent to create channels itself (e.g. the schedule board).
- **Per-channel access:** a bot only sees channels its role can view. To let an
  agent read/post in a private channel, add its role to that channel. A bot with
  no access **silently drops** messages there (and reads return `403`).

These fixes need only the web dashboard — no machine access.

## macOS privacy grants (desktop control)

The `computer` tool (screenshots, clicks, typing, window control) needs two
macOS TCC grants, human-only via **System Settings → Privacy & Security** —
they cannot be scripted. Everything chat-based works without them.

- **Screen Recording** → add the install's Python binary
  (`<install>/.venv/bin/python3`) — needed for screenshots
- **Accessibility** → same binary — needed for clicks/typing/System Events
- Add the **terminal app** too if kbots is run in the foreground: the grant
  attaches to whichever app hosts the process — and for the same reason,
  re-add the binary after a `.venv` rebuild that changes the Python binary
- First-time **Automation** prompts ("… wants to control …") appear per app
  on the Mac's screen — click Allow; they can't be pre-granted

Check from chat with `computer(action='perms')`; the install playbook covers
this as Phase 3.5. A `computer` action that *hangs* usually means a permission
prompt is sitting open on the Mac's screen.

## Chrome sessions — the sign-in-once flow

The `chrome_browser` tool drives a dedicated debug Chrome
(`scripts/chrome-debug.sh`). Web logins for agents are granted by **the user
signing in by hand**, never by agents handling credentials:

1. The agent calls `chrome_browser(action='login', profile='<service>',
   url='<sign-in page>')` — a visible Chrome window opens on a **named profile**
   dedicated to that identity/service.
2. The user types their password (and 2FA) directly into Chrome. Credentials
   never pass through the agent, the vault, or any config file.
3. The session persists in that profile across restarts; the agent drives it by
   passing `profile='<service>'` on subsequent actions.

**Revoking:** delete the profile's directory
(`~/.kbots-chrome-debug/<profile>`, or `$KBOTS_CHROME_DEBUG_DIR/<profile>`) —
the login is gone.

**One profile per identity/service.** All profiles share one Chrome instance
and one debug port, and Chrome doesn't label CDP targets with their profile —
a page that unexpectedly shows logged-out usually means the wrong profile, not
a lost session. `chrome_browser(action='status')` lists the profiles that
exist.

**Trust boundary:** the CDP debug port has no auth, so any process (and any
agent) on the machine that can reach it can drive any logged-in profile. A
sign-in therefore grants that session to the whole install, gated only by the
tool's consent + reservation checks — don't sign in accounts you wouldn't
trust every agent on the machine with. Chrome 136+ never exposes CDP on the
user's own default profile, so the user's real browser stays out of reach by
design.

## Diagnosis quick reference

| Symptom | Likely cause | Check |
|---|---|---|
| Every tool denied, all agents, "haven't granted it yet" | root-owned `~/.claude.json` (trust broken) | `find <home>/.claude.json <home>/.claude -not -user <service-user>` |
| Same, single agent | workspace missing/never trusted, or missing `.claude/settings.json` | boot log `permissions:` line; does the file exist? |
| Agent can't write in its `extra_dirs` repo | trust broken (above) — `extra_dirs` grants never apply while untrusted | same as first row |
| `uv sync`/`uv run` fails with cache write errors | root-owned `~/.cache/uv` | `find ~/.cache/uv -not -user <service-user>` |
| Bot silent in one channel only | Discord role can't view the channel | channel permission overrides in Discord |
| "OAuth session expired and could not be refreshed" | genuine token expiry, not ownership | re-login interactively as the service user |
| Worked earlier today, broke mid-session | a root `claude` session ran meanwhile | service log for `Cannot mark workspace trusted` |
| `computer` screenshots black/empty, or clicks do nothing / hang | missing macOS Screen Recording / Accessibility grant (a hang = prompt open on the Mac's screen) | `computer(action='perms')`; System Settings → Privacy & Security |

The definitive evidence is always the service log
(`journalctl -u kbots` / `<overlay>/data/launchd.stderr.log`): the engine names
the failure and prints the fix. For per-agent forensics, the CLI transcript
(`~/.claude/projects/<workspace-slug>/<session>.jsonl`) shows every tool denial
verbatim.

## Quick check

The startup preflight logs one `permissions:` line — `✓ permissions: …` when
clean, or a `⚠ permissions: …` per problem, each ending with the exact command
to run. Fix, then restart to re-check (`scripts/self-deploy.sh`, or
`/admin reboot`). While running, permission watch re-checks every `interval`
seconds and escalates anything new.
