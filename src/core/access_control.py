"""Access control — three-layer permission system.

Layer 1 — Can they talk?   (can_message)
  Sender tier + agent tier → message allowed or silently ignored.

Layer 2 — What tools?      (disallowed_tools_for_sender / disallowed_builtins_for_sender)
  Sender tier → --disallowedTools computed per message before CLI spawn.

Layer 3 — Agent ceiling     (agents.yaml — tools, disallow_builtins)
  Static config per agent. Not handled here.

People tiers (from team.json "access" field):
  owner   — message all agents, full tools
  admin   — message privileged + coordinator + assistant, safe tools + existing HITL, Edit/Write allowed, Bash blocked
  staff   — message assistant only, safe tools only, no CLI builtins
  unknown — message nobody, no tools

Agent tiers (from team.json "agent_tier" field):
  privileged  — full CLI + all MCP (dangerous = HITL), can message all agents
  coordinator — no CLI + all MCP (dangerous = HITL), can message all agents
  assistant   — no CLI + safe MCP only, cannot message other agents

Source of truth: config/team.json
Config: security.access_control in config.yaml
"""

import json
import logging

from src.core.base import resolve_config_file

logger = logging.getLogger(__name__)

TEAM_FILE = resolve_config_file("team.json")

# Which people tiers can message which agent tiers
_MESSAGE_RULES: dict[str, set[str]] = {
    "owner": {"privileged", "coordinator", "assistant"},
    "admin": {"privileged", "coordinator", "assistant"},
    "staff": {"assistant"},
    "unknown": set(),
}

# Which agent tiers can message which agent tiers
_AGENT_MESSAGE_RULES: dict[str, set[str]] = {
    "privileged": {"privileged", "coordinator", "assistant"},
    "coordinator": {"privileged", "coordinator", "assistant"},
    "assistant": set(),  # Cannot message any agents
}


def _load_team() -> dict:
    """Load team.json and build user_id → member lookup (Discord IDs and member IDs)."""
    try:
        data = json.loads(TEAM_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    lookup = {}
    for human in data.get("humans", []):
        entry = {
            "id": human["id"],
            "name": human.get("name", human["id"]),
            "type": "human",
            "access": human.get("access", "staff"),
        }
        discord_id = (human.get("contact") or {}).get("discord")
        if discord_id:
            lookup[discord_id] = entry
        lookup[human["id"]] = entry
    for agent in data.get("agents", []):
        entry = {
            "id": agent["id"],
            "name": agent.get("name", agent["id"]),
            "type": "agent",
            "access": "agent",
            "agent_tier": agent.get("agent_tier", "assistant"),
        }
        discord_id = agent.get("discord")
        if discord_id:
            lookup[discord_id] = entry
        lookup[agent["id"]] = entry
    return lookup


def _load_agent_tiers() -> dict[str, str]:
    """Load agent_id → agent_tier mapping from team.json."""
    try:
        data = json.loads(TEAM_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {
        a["id"]: a.get("agent_tier", "assistant")
        for a in data.get("agents", [])
    }


class AccessControl:
    """Three-layer access control for kbots agents."""

    def __init__(self, config: dict, hitl_gated_tools: list[str] | None = None,
                 admin_users: list[str] | None = None):
        """
        config: security.access_control block from config.yaml
        hitl_gated_tools: tools with HITL gates in MCP (from security.hitl.gated_tools).
                          Agent-originated calls to these tools pass through to MCP
                          where the HITL gate fires, instead of being hard-blocked.
        admin_users: config.admin_users.discord — always resolved to owner tier so
                     that enabling access control can never lock the operator out,
                     even if they aren't listed in team.json.

        Expected shape:
            safe_tools: [list of tools any known tier can use freely]
            unknown_policy: deny | safe_only  (default: deny)
        """
        self._safe_tools: set[str] = set(config.get("safe_tools", []))
        self._hitl_gated: set[str] = set(hitl_gated_tools or [])
        self._unknown_policy: str = config.get("unknown_policy", "deny")
        self._admin_users: set[str] = set(str(u) for u in (admin_users or []))
        self._team: dict = _load_team()
        self._agent_tiers: dict[str, str] = _load_agent_tiers()

    def reload_team(self) -> None:
        """Reload team.json (call after team_update)."""
        self._team = _load_team()
        self._agent_tiers = _load_agent_tiers()

    def resolve_tier(self, user_id: str, is_bot: bool = False) -> str:
        """Return access tier for a discord user ID."""
        member = self._team.get(user_id)
        if not member:
            # Configured admins are owner even if absent from team.json — this is
            # what keeps enabling access control from locking the operator out.
            if not is_bot and str(user_id) in self._admin_users:
                return "owner"
            return "unknown"
        if member["type"] == "agent" or is_bot:
            return "agent"
        # A human who is also a configured admin gets at least owner privileges.
        if str(user_id) in self._admin_users:
            return "owner"
        return member.get("access", "staff")

    def get_sender_agent_tier(self, user_id: str) -> str | None:
        """Return agent_tier if sender is an agent, else None."""
        member = self._team.get(user_id)
        if member and member["type"] == "agent":
            return member.get("agent_tier", "assistant")
        return None

    def get_agent_tier(self, agent_id: str) -> str:
        """Return agent_tier for an agent by its config ID."""
        return self._agent_tiers.get(agent_id, "assistant")

    # --- Layer 1: Can they talk? ---

    def can_message(self, user_id: str, agent_id: str,
                    is_bot: bool = False) -> bool:
        """Check if sender is allowed to message this agent at all.

        Returns True if the message should be processed, False to silently ignore.
        """
        target_tier = self.get_agent_tier(agent_id)

        # Is sender a bot/agent?
        if is_bot or self.resolve_tier(user_id) == "agent":
            sender_agent_tier = self.get_sender_agent_tier(user_id)
            if not sender_agent_tier:
                logger.info(
                    f"Access denied: sender={user_id} sender_tier=unknown-bot "
                    f"→ target={agent_id} target_tier={target_tier}"
                )
                return False
            allowed_targets = _AGENT_MESSAGE_RULES.get(sender_agent_tier, set())
            allowed = target_tier in allowed_targets
            if not allowed:
                logger.info(
                    f"Access denied: sender={user_id} sender_tier={sender_agent_tier} "
                    f"→ target={agent_id} target_tier={target_tier}"
                )
            return allowed

        # Human sender
        sender_tier = self.resolve_tier(user_id)
        allowed_targets = _MESSAGE_RULES.get(sender_tier, set())
        allowed = target_tier in allowed_targets
        if not allowed:
            logger.info(
                f"Access denied: sender={user_id} sender_tier={sender_tier} "
                f"→ target={agent_id} target_tier={target_tier}"
            )
        return allowed

    # --- Layer 2: What tools? ---

    def check(self, user_id: str, tool_name: str, is_bot: bool = False,
              agent_id: str | None = None) -> dict:
        """Check if user_id is allowed to trigger tool_name.

        Returns:
            {"allowed": True} or
            {"allowed": False, "reason": str, "gate": "hitl"|"deny"}
        """
        tier = self.resolve_tier(user_id, is_bot)
        agent_tier = self.get_agent_tier(agent_id) if agent_id else None

        # --- Isolated agent check (assistant tier cannot cross-agent) ---
        if agent_tier == "assistant":
            cross_agent_tools = {"send_to_agent", "send_message", "ask_agent"}
            if tool_name in cross_agent_tools:
                return {
                    "allowed": False,
                    "reason": f"Assistant-tier agent '{agent_id}' cannot use '{tool_name}'.",
                    "gate": "deny",
                }

        # --- Owner: full access ---
        if tier == "owner":
            return {"allowed": True}

        # --- Safe tools: allowed for all known tiers ---
        if tool_name in self._safe_tools:
            if tier == "unknown":
                if self._unknown_policy == "safe_only":
                    return {"allowed": True}
                return {
                    "allowed": False,
                    "reason": f"Unknown user '{user_id}' — tool access denied.",
                    "gate": "deny",
                }
            return {"allowed": True}

        # --- Non-safe tools by tier ---
        if tier == "admin":
            # Admins can use non-safe tools, but existing HITL still applies
            return {"allowed": True}

        if tier == "staff":
            return {
                "allowed": False,
                "reason": f"Staff users can only use safe tools. '{tool_name}' requires owner/admin.",
                "gate": "deny",
            }

        if tier == "agent":
            return {
                "allowed": False,
                "reason": f"Bot-originated request — '{tool_name}' requires HITL approval.",
                "gate": "hitl",
            }

        # Unknown
        return {
            "allowed": False,
            "reason": f"Unknown user '{user_id}' — tool access denied.",
            "gate": "deny",
        }

    def disallowed_tools_for_sender(
        self, user_id: str, all_tool_names: list[str],
        is_bot: bool = False, agent_id: str | None = None,
    ) -> list[str]:
        """Return MCP tool names to block via --disallowedTools for this sender.

        Used before spawning Claude Code CLI to hard-block tools the sender
        isn't allowed to trigger. Returns tool names WITHOUT the mcp__ prefix.
        """
        tier = self.resolve_tier(user_id, is_bot)
        agent_tier = self.get_agent_tier(agent_id) if agent_id else None

        # Assistant-tier agents: always block cross-agent tools
        cross_agent = {"send_to_agent", "send_message", "ask_agent"}
        assistant_blocked = []
        if agent_tier == "assistant":
            assistant_blocked = [t for t in all_tool_names if t in cross_agent]

        # Owner: no additional blocks (just assistant isolation if applicable)
        if tier == "owner":
            return assistant_blocked

        # Admin: safe tools + existing HITL, no additional blocks from AC
        # (HITL config handles dangerous tools)
        if tier == "admin":
            return assistant_blocked

        # Agent tier: block non-safe tools, but let HITL-gated tools through
        # so the MCP HITL gate can fire and prompt for approval.
        if tier == "agent":
            blocked = []
            for t in all_tool_names:
                if t in self._safe_tools:
                    continue
                if t in self._hitl_gated:
                    continue  # Let MCP HITL gate handle approval
                blocked.append(t)
            if agent_tier == "assistant":
                for t in cross_agent:
                    if t not in blocked:
                        blocked.append(t)
            return blocked

        # Staff: block everything not in safe list
        blocked = []
        for t in all_tool_names:
            if t in self._safe_tools:
                continue
            blocked.append(t)

        # Assistant-tier: also block cross-agent even if "safe"
        if agent_tier == "assistant":
            for t in cross_agent:
                if t not in blocked:
                    blocked.append(t)

        # Unknown: block everything
        if tier == "unknown":
            return list(all_tool_names)

        return blocked

    def disallowed_builtins_for_sender(
        self, user_id: str, is_bot: bool = False,
    ) -> list[str]:
        """Return Claude Code built-in tool names to block for this sender.

        Owner: no blocks (agent's own disallow_builtins still applies).
        Admin: Edit/Write/MultiEdit allowed, Bash blocked.
        Staff/agent/unknown: all CLI builtins blocked.
        """
        tier = self.resolve_tier(user_id, is_bot)
        if tier == "owner":
            return []
        if tier == "admin":
            return ["Bash"]
        # Staff, agent, unknown: no CLI builtins
        return ["Edit", "Write", "Bash", "MultiEdit"]
