"""set_reply_shorten — turn reply shortening on or off without a restart.

Admin-gated, for the same reason set_hitl is: an agent that can switch off its
own length limit will switch it off. Reading the current setting is open to
anyone, because knowing whether your reply will be cut is not a privilege.

Scope: with no agent named, the change is fleet-wide. With one named, it
applies to that agent only and beats the fleet-wide value, which is what makes
it usable for the case this exists to solve, an agent whose deliverable is the
prose itself.
"""

import logging

from src.core import runtime_state
from src.core.base import ToolContext
from src.core.tools import tool
from src.tools.hitl_admin import _is_admin

logger = logging.getLogger(__name__)


def _key(agent: str) -> str:
    return f"reply_shorten:{agent}" if agent else "reply_shorten"


@tool(
    name="set_reply_shorten",
    description=(
        "Turn reply shortening ON or off, and set the length that triggers it. "
        "When ON, a long reply is cut at a section boundary and the rest "
        "arrives when the reader reacts 🔍 or says 'more'. Applies live, no "
        "restart. Pass an agent name to change one agent only, or leave it "
        "empty for the whole fleet. Admin-only to change; anyone may read the "
        "current setting with enabled unset."
    ),
    category="admin",
)
async def set_reply_shorten(ctx: ToolContext, enabled: bool | None = None,
                            threshold_chars: int = 0, agent: str = "") -> str:
    """Change or read the reply-shortening setting.

    Args:
        enabled: True to shorten long replies, False to send them whole. Leave
            unset to read the current setting without changing it.
        threshold_chars: Replies longer than this get shortened. 0 leaves it.
        agent: Agent name to scope the change to. Empty means the whole fleet.
    """
    current = dict(runtime_state.get_flag(_key(agent), None) or {})

    if enabled is None and not threshold_chars:
        scope = f"agent '{agent}'" if agent else "the fleet"
        if not current:
            return (f"No runtime override for {scope} — it is using the value "
                    f"from config (defaults.reply.shorten, or that agent's own "
                    f"reply.shorten in agents.yaml).")
        return f"Runtime override for {scope}: {current}"

    if not _is_admin(ctx.user_id or ""):
        return ("ERROR: only an admin can change reply shortening. "
                "(This is deliberate — an agent must not be able to switch off "
                "its own length limit.)")

    if threshold_chars and threshold_chars < 100:
        return (f"ERROR: {threshold_chars} is too small a threshold. Below ~100 "
                f"characters there is no room for a conclusion before the cut, "
                f"so every reply would arrive as a fragment.")

    if enabled is not None:
        current["enabled"] = bool(enabled)
    if threshold_chars:
        current["threshold_chars"] = int(threshold_chars)
    runtime_state.set_flag(_key(agent), current)

    scope = f"**{agent}**" if agent else "the whole fleet"
    state = "ON" if current.get("enabled") else "OFF"
    where = f" over {current['threshold_chars']} chars" if current.get("threshold_chars") else ""
    logger.warning(f"Reply shortening set {state} for {agent or 'fleet'} "
                   f"by admin {ctx.agent_id} (user {ctx.user_id})")
    return (f"✂️ Reply shortening is now **{state}**{where} for {scope}. "
            f"Applies from the next message, no restart.")
