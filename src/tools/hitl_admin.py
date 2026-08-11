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
