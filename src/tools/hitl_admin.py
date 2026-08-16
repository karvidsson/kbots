"""set_hitl — let an admin turn the human-approval gate on/off by asking the
agent in plain language (complements the /admin hitl slash command).

Admin-gated: only a configured admin (admin_users.discord) can flip it, so an
agent can't silently disable its own oversight on a random user's say-so.
"""

import logging

import yaml

from src.core import runtime_state
from src.core.base import ToolContext, resolve_config_file
from src.core.tools import tool

logger = logging.getLogger(__name__)


def _is_admin(user_id: str) -> bool:
    cfg_file = resolve_config_file("config.yaml")
    if not user_id or not cfg_file.exists():
        return False
    try:
        cfg = yaml.safe_load(cfg_file.read_text()) or {}
    except yaml.YAMLError:
        return False
    admins = cfg.get("admin_users", {}).get("discord", []) or []
    return str(user_id) in [str(a) for a in admins]


@tool(
    name="set_hitl",
    description=(
        "Turn the human-approval (HITL) gate ON or off. When ON, sensitive/"
        "destructive tools require your approval; when off, the agent acts "
        "autonomously. Admin-only — only a configured admin can flip it."
    ),
    category="admin",
)
async def set_hitl(ctx: ToolContext, enabled: bool) -> str:
    """Flip the approval gate. enabled=false turns off approvals."""
    if not _is_admin(ctx.user_id or ""):
        return ("ERROR: only an admin can change the approval gate. "
                "(This is deliberate — an agent can't disable its own oversight.)")
    runtime_state.set_flag("hitl_enabled", bool(enabled))
    state = "ON" if enabled else "OFF"
    logger.warning(f"HITL gate set {state} by admin {ctx.agent_id} (user {ctx.user_id})")
    note = "" if enabled else " — I'll now act without approval prompts."
    return (f"🔓 Human-approval gate is now **{state}**{note} "
            f"(applies from your next message; also `/admin hitl {state.lower()}`).")


@tool(
    name="set_hitl_channel",
    description=(
        "Point human-approval (HITL) requests at a Discord channel — approval "
        "cards post there and an admin reacts ✅/❌. Pass a channel_id to enable "
        "it (create a private one first with discord_create_channel if needed), "
        "or an empty channel_id to clear the override (reverts to config; with "
        "no config channel, gated tools are denied fail-closed). Applies live — "
        "no restart. Admin-only."
    ),
    category="admin",
)
async def set_hitl_channel(ctx: ToolContext, channel_id: str) -> str:
    """Point approvals at channel_id; empty clears the override. Live."""
    if not _is_admin(ctx.user_id or ""):
        return ("ERROR: only an admin can set the approval channel "
                "(it decides who sees and approves sensitive tool calls).")
    channel_id = (channel_id or "").strip()
    if not channel_id:
        runtime_state.clear_flag("hitl_channel")
        logger.warning(f"HITL channel override cleared by admin {ctx.agent_id} (user {ctx.user_id})")
        return ("🔐 Cleared the approval-channel override — HITL now follows config "
                "(security.hitl.channel). If config has none, approval-gated tools "
                "are denied fail-closed until a channel is set.")
    if not channel_id.isdigit():
        return f"ERROR: '{channel_id}' is not a Discord channel ID (numeric)."
    runtime_state.set_flag("hitl_channel", channel_id)
    logger.warning(f"HITL channel → {channel_id} by admin {ctx.agent_id} (user {ctx.user_id})")
    return (
        f"🔐 Approval requests now post to <#{channel_id}> — applies immediately, no restart.\n"
        f"How it works: when an agent calls an approval-gated tool, a card appears there "
        f"describing the call; an admin reacts ✅ to approve or ❌ to deny. No reaction "
        f"within the timeout (default 30 min) = denied. Explain this to the user now."
    )
