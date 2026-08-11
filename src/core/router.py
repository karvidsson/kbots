"""Message router — routes incoming messages from connectors to agents based on config."""

import logging
from typing import TYPE_CHECKING

from src.core.base import IncomingMessage

if TYPE_CHECKING:
    from src.core.agent_manager import AgentManager

logger = logging.getLogger(__name__)


class Router:
    """Routes messages from connectors to the right agent."""

    def __init__(self, agent_manager: "AgentManager"):
        self.agent_manager = agent_manager

    async def route(self, message: IncomingMessage) -> None:
        """Route an incoming message to the appropriate agent(s)."""
        matched = self._find_agents(message)

        if not matched:
            logger.debug(
                f"No agent matched for message in {message.connector}:{message.channel_id} "
                f"from {message.user_name}"
            )
            return

        for agent_id in matched:
            try:
                await self.agent_manager.handle_message(agent_id, message)
            except Exception as e:
                logger.error(f"Error routing to agent {agent_id}: {e}", exc_info=True)

    def _find_agents(self, message: IncomingMessage) -> list[str]:
        """Find which agents should receive this message based on routing config."""
        matched = []

        for agent_id, agent_cfg in self.agent_manager.agent_configs.items():
            routing = agent_cfg.get("routing", {})
            connector_routing = routing.get(message.connector, {})

            if not connector_routing:
                continue

            if self._matches_routing(message, connector_routing):
                matched.append(agent_id)

        return matched

    def _matches_routing(self, message: IncomingMessage, routing: dict) -> bool:
        """Check if a message matches a routing config block."""
        # Bot account filter — only route to agents bound to the bot that received this message
        agent_account = routing.get("account")
        if agent_account and message.raw:
            bot_account = getattr(message, "bot_account", None)
            if bot_account and agent_account != bot_account:
                return False

        # Channel filter — empty list means all channels
        channels = routing.get("channels", [])
        if channels and message.channel_id not in channels:
            return False

        # Guild filter (Discord-specific)
        guilds = routing.get("guilds", [])
        if guilds and message.raw:
            guild_id = getattr(getattr(message.raw, "guild", None), "id", None)
            if guild_id and str(guild_id) not in guilds:
                return False

        # Mention filter — if mentions: true, only route if bot was mentioned
        # This is checked by the connector before emitting (sets a flag or filters)
        # Here we just accept if it passed the connector's filter
        mentions_only = routing.get("mentions", False)
        if mentions_only:
            # The connector should only emit messages where the bot was mentioned
            # or it was a DM. We trust the connector's filtering here.
            pass

        return True
