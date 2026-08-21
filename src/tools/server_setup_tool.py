"""setup_discord_server — provision the channels a kbots fleet needs, by hand.

The same routine the guild-join hook runs. Exposed as a tool for the two cases
the hook cannot cover: a server the bot was already in before this shipped, and
a re-run after the owner grants the Manage Channels permission that a first
attempt was missing.

Admin-only. It creates channels in someone's server and repoints where approval
requests land, which is not something a passing user should be able to ask for.
"""

import logging

import yaml

from src.core import server_setup
from src.core.base import ToolContext, resolve_config_file
from src.core.tools import tool

logger = logging.getLogger(__name__)


def _load_config() -> dict:
    cfg_file = resolve_config_file("config.yaml")
    if not cfg_file.exists():
        return {}
    try:
        return yaml.safe_load(cfg_file.read_text()) or {}
    except yaml.YAMLError:
        return {}


def _is_admin(user_id: str, config: dict) -> bool:
    if not user_id:
        return False
    admins = (config.get("admin_users", {}) or {}).get("discord", []) or []
    return str(user_id) in [str(a) for a in admins]


@tool(
    name="setup_discord_server",
    description=(
        "Provision the channels a kbots fleet needs in a Discord server: "
        "approvals, schedules, alerts, platform-updates and a goals category. "
        "Adopts channels that already exist by name, creates only what is "
        "missing, and never repoints a role that is already configured. Wires "
        "the IDs live, no restart. Safe to re-run. Admin-only."
    ),
    category="admin",
)
async def setup_discord_server(ctx: ToolContext, guild_id: str, bot: str = "") -> str:
    """Create/adopt the fleet channels in guild_id and wire their IDs live.

    Args:
        guild_id: The Discord server (guild) ID to set up.
        bot: Which bot account's token to use (e.g. 'main'). Empty for default.
    """
    config = _load_config()
    if not _is_admin(ctx.user_id or "", config):
        return ("ERROR: only an admin can set up a server — this creates channels "
                "and decides where approval requests are seen.")

    guild_id = (guild_id or "").strip()
    if not guild_id.isdigit():
        return f"ERROR: '{guild_id}' is not a Discord server ID (numeric)."

    if not ctx.vault:
        return "ERROR: no vault access, so no bot token to act with."
    token = ""
    if bot:
        token = ctx.vault.get(f"discord-{bot}") or ctx.vault.get(f"discord-token-{bot}") or ""
    else:
        token = ctx.vault.get("active-discord-token") or ctx.vault.get("discord-token") or ""
    if not token:
        return f"ERROR: no Discord token found for bot '{bot or 'main'}'."

    outcomes = await server_setup.provision_guild(guild_id, token, config)
    server_setup.wire(outcomes)

    failed = [o for o in outcomes if o.action == "failed"]
    header = ("⚠️ Server setup finished with failures" if failed
              else "✅ Server setup complete")
    tail = ""
    if failed:
        tail = ("\n\nNothing that failed was left half-wired: those roles are "
                "still unset, so re-running this after fixing the cause is safe.")
    logger.info(f"setup_discord_server({guild_id}) by {ctx.agent_id}: "
                + ", ".join(f"{o.key}={o.action}" for o in outcomes))
    return f"{header} for server {guild_id}:\n\n{server_setup.summarize(outcomes)}{tail}"
