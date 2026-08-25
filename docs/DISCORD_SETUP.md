# Discord Bot Setup

Every kbots agent speaks through a Discord **bot account** — one Discord
*application* per bot. This guide is the full reference for creating those
applications correctly: the permissions kbots actually needs (and the ones it
must never have), where install links come from, and how to fix a bot that was
invited with too much power.

The [README Quickstart](../README.md#quickstart--zero-to-agent) has the short
version — including a Claude-in-Chrome prompt that does the portal clicking for
you. Read this document when you're adding more bots, tightening an existing
one, or wondering why a permission is on the list.

## One application per bot

- **Main agent's bot** — the *setup account*. It provisions the fleet channels
  on a server (see [Server auto-setup](#server-auto-setup)), so it alone gets
  the **Manage Channels** permission.
- **Every other bot** (ops agent, extra agents) — the minimal set only.

Tokens go straight into the wizard, which stores them in the encrypted vault.
A token grants full control of its bot account: never paste one anywhere but a
hidden prompt, and reset it in the portal if it ever lands in a file, a chat,
or terminal scrollback.

## Creating the application

1. [Discord Developer Portal](https://discord.com/developers/applications) →
   **New Application** → name it (this becomes the agent's identity).
2. **Bot** tab → **Reset Token** → copy it. *This is the one secret the wizard
   asks for.*
3. Still on the Bot tab, under **Privileged Gateway Intents**, enable
   **Message Content Intent**. ⚠️ *The #1 setup mistake — without it the bot
   connects fine and silently ignores every message.* Enable **Server Members
   Intent** too.

## Permissions: what kbots needs, and why

| Permission | Why kbots needs it |
|---|---|
| View Channels | see the channels it serves |
| Send Messages | reply |
| Read Message History | thread context, catching up after a restart |
| Add Reactions | HITL approval cards (✅/❌), reply-shortener "show more" |
| Attach Files | send charts, PDFs, screenshots |
| Embed Links | rich links in replies |
| Change Nickname | identity boot — the agent sets its own server nickname |
| Manage Channels | **main bot only** — server auto-setup creates the fleet channels |

That is the complete list. In particular:

> **Never grant Administrator or Manage Server.** kbots uses neither.
> Administrator supersedes every other permission *and bypasses channel-level
> overrides*, so a bot holding it is one leaked token or one prompt-injected
> action away from full server control — banning members, deleting channels,
> editing every role. If Administrator is checked, every other checkbox on the
> authorize screen is decoration.

## Install links

**The wizard prints one for you.** After each bot token is stored (main bot,
ops instance, extra bots), setup prints that bot's install link with the right
permissions baked in, and the final summary lists them all:

```
https://discord.com/oauth2/authorize?client_id=<app-id>&scope=bot%20applications.commands&permissions=67226704
```

`permissions=67226704` is the table above with Manage Channels (the main bot);
`67226688` is the same set without it (every other bot). An explicit
`permissions=` parameter in the URL **overrides** the app's default install
settings — so the wizard's link requests the right set no matter what the
portal says.

**The portal's own link is a trap by default.** On the application's
**Installation** page, the "Discord Provided Link" is bare
(`client_id` only). Opening it makes Discord fall back to the app's **Default
Install Settings** — and new applications frequently end up with
**Administrator** sitting there. If you use that link, fix the page first:

- **Installation Contexts**: *Guild Install* only (kbots is a server bot, not
  a user app).
- **Default Install Settings → Scopes**: `bot` + `applications.commands`.
- **Default Install Settings → Permissions**: remove *Administrator*, add the
  table above (+ *Manage Channels* if this is the main bot).

## Already invited with too much?

Install settings only affect **future** installs. A bot that was added while
its link (or defaults) requested Administrator *keeps* Administrator in that
server — nothing shrinks it retroactively.

Fix it in place, no re-invite needed: **Server Settings → Roles →** the bot's
auto-created role (it carries the bot's name) → remove Administrator / Manage
Server, and make sure the minimal set from the table is granted. The change
takes effect immediately.

## Server auto-setup

When the **main** bot joins a server while the service is running, kbots
provisions the channels a fleet needs — `#kbots-approvals`,
`#kbots-schedules`, `#kbots-alerts`, `#platform-updates`, and a `kbots-goals`
category — and wires their IDs live. Existing channels with matching names are
adopted, never duplicated; anything already configured is never repointed; the
whole thing is idempotent.

Two things to know:

- It needs **Manage Channels**. Without it, each channel the bot could not
  create is reported with the reason, and the rest still wire up.
- The `guild_join` event only fires while the bot is **online**. If the bot
  was invited before the service first booted (the normal quickstart order),
  ask your main agent to run **`setup_discord_server`** with the server ID —
  it executes the exact same idempotent provisioning.

## Collecting IDs

Enable **Developer Mode** first (Discord Settings → Advanced → Developer
Mode), then right-click → Copy ID:

- your **server** → Server ID (the wizard's guild prompt)
- **yourself** → User ID (makes you the owner/admin)
- the **approvals channel** → Channel ID (where HITL prompts land)

Or read server and channel IDs straight from a channel URL:
`discord.com/channels/<server-id>/<channel-id>`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Bot connects but ignores every message | Message Content Intent off | Bot tab → Privileged Gateway Intents → enable, then restart the service |
| Auto-setup reports channels it "could not create" | Bot role lacks Manage Channels | Grant it on the bot's role in Server Settings, then re-run `setup_discord_server` |
| Bot shows online but nothing ever answers | No agent routes to that bot account | Point an agent at it: `agents.yaml` → `routing.discord.account` (the boot log warns about unserved accounts) |
| Authorize screen asks for Administrator | App's Default Install Settings | Use the wizard's install link, and clean the Installation page ([above](#install-links)) |
| Slash commands missing | Global command sync is slow | `/admin sync` in Discord, or wait up to an hour |
