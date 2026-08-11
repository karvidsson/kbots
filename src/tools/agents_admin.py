"""Agent administration tools — let the main agent create further agents.

Design: the setup wizard creates only the process and the first ("main")
agent. Additional agents are requested from the main agent, which calls
create_agent (HITL-gated). The new agent comes online after a restart.

Policy: **one Discord app per agent.** Every agent gets its own bot account
(own name, avatar, and member-list entry) — sharing a bot between agents is
refused. Discord has no API for creating applications, so the result
includes a ready-to-paste browser-automation prompt (Claude in Chrome)
pre-filled with the bot name and server ID, plus instructions for storing
the token. The config accounts entry is written automatically.
"""

import logging
import os
from pathlib import Path

import yaml

from src.core.agent_scaffold import (
    TIERS,
    agent_entries,
    agent_is_privileged_or_coordinator,
    scaffold_agent,
)
from src.core.base import ToolContext, resolve_config_file
from src.core.tools import tool

logger = logging.getLogger(__name__)

PORTAL_PROMPT_TEMPLATE = """\
Set up a Discord bot for me in the Discord Developer Portal
(https://discord.com/developers/applications). I'm logged in.

1. Create a New Application named "{display_name}".
2. On the Bot tab: enable "Message Content Intent" and "Server Members
   Intent" under Privileged Gateway Intents, and save.
3. On the Bot tab, click "Reset Token", then STOP — do NOT read, copy,
   store, or repeat the token. Leave it visible on screen and tell me to
   copy it myself. Wait for me to confirm before continuing.
4. Go to OAuth2 -> URL Generator: select scopes "bot" and
   "applications.commands"; under Bot Permissions select: View Channels,
   Send Messages, Read Message History, Add Reactions, Attach Files,
   Embed Links, and — for the MAIN agent — Manage Channels (so it can
   create channels itself, e.g. the platform-updates notices channel).
   Open the generated URL and add the bot to {server_ref}.
5. Confirm the bot appears in the server's member list, then give me a
   checklist summary of what was done."""


def _hub_agent_id(overlay: Path, exclude: str = "") -> str:
    """The 'center' agent others report to: the coordinator, else the agent on
    the 'main' Discord account. Empty if none found."""
    entries = agent_entries(overlay)
    for aid, cfg in entries.items():
        if aid != exclude and cfg.get("tier") == "coordinator":
            return aid
    for aid, cfg in entries.items():
        if aid == exclude:
            continue
        if ((cfg.get("routing") or {}).get("discord") or {}).get("account") == "main":
            return aid
    return ""


def _caller_may_create(overlay: Path, caller_id: str) -> bool:
    """Only coordinator/privileged agents (i.e. the main/ops agents) may
    create agents — assistants cannot reach the orchestrator's admin tools."""
    return agent_is_privileged_or_coordinator(overlay, caller_id)


def _accounts_in_use(overlay: Path) -> dict[str, str]:
    """Map bot_account -> agent_id for every agent across agents*.yaml files."""
    used: dict[str, str] = {}
    for agent_id, agent_cfg in agent_entries(overlay).items():
        account = (
            (agent_cfg or {}).get("routing", {}).get("discord", {}).get("account")
        )
        if account:
            used.setdefault(account, agent_id)
    return used


def _portal_prompt(display_name: str, guild_id: str) -> str:
    if guild_id:
        server_ref = (
            f"the server with ID {guild_id} "
            f"(open https://discord.com/channels/{guild_id} to identify it)"
        )
    else:
        server_ref = "my server (ask me which one)"
    return PORTAL_PROMPT_TEMPLATE.format(display_name=display_name, server_ref=server_ref)


@tool(
    name="create_agent",
    description=(
        "Create a new agent: agents.yaml entry, agent directory, CLAUDE.md, "
        "MCP and permission config. Requires human approval; the new agent "
        "comes online after the service restarts. Every agent gets its OWN "
        "Discord bot identity (one Discord app per agent — sharing is "
        "refused). The result includes a ready-to-paste Claude-in-Chrome "
        "prompt that automates the Discord Developer Portal setup, "
        "pre-filled with the bot name and server ID, plus where the token "
        "goes. bot_account defaults to the agent name."
    ),
    category="admin",
    hitl=True,
)
async def create_agent(
    ctx: ToolContext,
    name: str,
    display_name: str,
    description: str,
    model: str = "sonnet",
    tier: str = "assistant",
    personality: str = "",
    channels: str = "",
    category_id: str = "",
    bot_account: str = "",
) -> str:
    """Scaffold a new agent in the overlay.

    name: internal id (lowercase, no spaces). tier: assistant (MCP tools only),
    coordinator (adds restricted shell), or privileged (full access — avoid
    unless explicitly requested). channels/category_id: comma-separated Discord
    IDs to route to; empty means all channels, mention-only. bot_account:
    name for the agent's own bot account (defaults to the agent name); must
    not already be used by another agent — one Discord app per agent.
    """
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    if not overlay:
        return "ERROR: KBOTS_OVERLAY is not set — cannot locate the deployment overlay."

    if not _caller_may_create(Path(overlay), ctx.agent_id):
        return (
            f"ERROR: agent '{ctx.agent_id}' is not allowed to create agents — "
            f"only the main (coordinator) or a privileged agent can. Ask the "
            f"main agent instead. (If this IS the main agent on an older "
            f"install, add 'tier: coordinator' to its agents.yaml entry.)"
        )

    if tier not in TIERS:
        return f"ERROR: invalid tier {tier!r} — one of {', '.join(TIERS)}."

    # Resolve bot account against config.yaml.
    config_path = resolve_config_file("config.yaml")
    cfg: dict = {}
    if config_path.exists():
        cfg = yaml.safe_load(config_path.read_text()) or {}
    discord_cfg = cfg.get("connectors", {}).get("discord", {}) or {}
    accounts: dict = discord_cfg.get("accounts", {}) or {}
    guild_id = str(discord_cfg.get("guild_id", "") or "")

    # One Discord app per agent: default the account to the agent name and
    # refuse accounts already bound to another agent.
    if not bot_account:
        bot_account = name
    used_by = _accounts_in_use(Path(overlay))
    if bot_account in used_by:
        return (
            f"ERROR: bot account '{bot_account}' is already used by agent "
            f"'{used_by[bot_account]}'. Policy is one Discord app per agent — "
            f"pass a new bot_account (default: the agent name)."
        )
    new_bot = bot_account not in accounts

    routing: dict = {
        "discord": {
            "account": bot_account,
            "channels": [c.strip() for c in channels.split(",") if c.strip()],
            "mentions": True,
        }
    }
    cats = [c.strip() for c in category_id.split(",") if c.strip()]
    if cats:
        routing["discord"]["categories"] = cats

    try:
        written = scaffold_agent(
            Path(overlay),
            name,
            display_name,
            description,
            model=model,
            tier=tier,
            personality=personality,
            routing=routing,
            bot_account=bot_account,
            kbots_modules=os.environ.get("KBOTS_MODULES", ""),
        )
    except ValueError as e:
        return f"ERROR: {e}"

    # Auto-register in the roster (team.json) so the relations map stays in sync
    # with agents.yaml — no manual team_add needed. New agents report to the hub
    # (the coordinator, else the 'main'-account agent).
    try:
        from src.tools.team import upsert_agent
        upsert_agent(name, display_name, agent_tier=tier, role=description[:120],
                     reports_to=_hub_agent_id(Path(overlay), exclude=name))
    except Exception as e:
        logger.warning(f"create_agent: roster auto-sync failed: {e}")

    logger.info(f"create_agent: '{name}' scaffolded by agent {ctx.agent_id} "
                f"(bot_account={bot_account}, new_bot={new_bot})")
    files = "\n".join(f"  - {p}" for p in written)
    result = (
        f"Agent '{display_name}' ({name}, {tier}, {model}) created:\n{files}\n"
    )

    if new_bot:
        # Register the account in config.yaml so only the token is missing.
        vault_key = f"discord-{bot_account}"
        if config_path.exists():
            cfg.setdefault("connectors", {}).setdefault("discord", {}) \
               .setdefault("accounts", {})[bot_account] = {"token_key": vault_key}
            config_path.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
            result += (
                f"Bot account '{bot_account}' added to config.yaml "
                f"(token_key: {vault_key}).\n"
            )

        prompt = _portal_prompt(display_name, guild_id)
        result += (
            f"\nThis agent gets its OWN Discord identity ('{bot_account}') — Discord "
            f"has no API for creating apps, so a human finishes it in ~2 minutes:\n\n"
            f"STEP 1 — paste this into Claude in Chrome (it automates the portal):\n"
            f"---\n{prompt}\n---\n\n"
            f"STEP 2 — store the token you copied (never paste it in chat):\n"
            f"  run in the install dir:  uv run python vault-manage.py\n"
            f"  add secret:              {vault_key}\n\n"
            f"STEP 3 — restart to bring the agent online: /admin reboot\n"
        )
    else:
        # Account already registered in config (e.g. pre-created bot) but not
        # bound to any agent — no portal work needed, just the token check.
        token_key = accounts.get(bot_account, {}).get("token_key", f"discord-{bot_account}")
        result += (
            f"Uses pre-registered bot account '{bot_account}'. Ensure its token "
            f"is in the vault (key: {token_key}), then restart: /admin reboot "
            f"(or scripts/update.sh)."
        )

    return result


# Only these key prefixes may be written via the tool — bot tokens and API keys.
# Anything else (vault-key material, internal state) is refused.
_VAULT_SET_ALLOWED = ("discord-", "secrets/")


@tool(
    name="vault_set_secret",
    description=(
        "Store a secret in the encrypted vault — used to save a Discord bot "
        "token the user pastes (key 'discord-<bot>'), or an API key "
        "('secrets/<name>'). Requires human approval. ONLY 'discord-*' and "
        "'secrets/*' keys are accepted. The value is encrypted at rest and "
        "redacted from all logs; never repeat it back. After storing a token, "
        "tell the user to delete their message containing the raw value, and "
        "that the bot comes online after /admin reboot."
    ),
    category="admin",
    hitl=True,
)
async def vault_set_secret(ctx: ToolContext, key: str, secret: str) -> str:
    """Write a secret to the encrypted vault.

    key: vault key — must start with 'discord-' (a bot token) or 'secrets/'
        (an API key). Any other key is refused.
    secret: the value to store (e.g. a pasted bot token). Redacted from logs;
        the arg name 'secret' triggers redaction in the audit + HITL paths.
    """
    key = (key or "").strip()
    if not key.startswith(_VAULT_SET_ALLOWED):
        allowed = " or ".join(repr(p) for p in _VAULT_SET_ALLOWED)
        return f"Refused: key '{key}' not allowed. Only keys starting with {allowed} may be set here."
    if not (secret or "").strip():
        return "Refused: empty value."
    if not ctx.vault:
        return "No vault available in this context."
    try:
        ctx.vault.set(key, secret)
    except Exception as e:
        return f"Failed to store '{key}': {e}"
    return (
        f"Stored '{key}' ({len(secret)} chars), encrypted at rest. "
        f"Delete your message with the raw value; the bot connects after /admin reboot."
    )
