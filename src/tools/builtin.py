"""Built-in tools — always available to agents."""

import logging
import os

from src.core.base import IncomingMessage, ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

# Maximum inter-agent call depth to prevent infinite loops (A -> B -> A -> ...)
MAX_INTER_AGENT_DEPTH = 3


def _env_depth() -> int:
    """This process's inter-agent depth, threaded in via subprocess env."""
    try:
        return int(os.environ.get("KBOTS_INTER_AGENT_DEPTH", "0") or 0)
    except ValueError:
        return 0


async def _loopback_agent_call(
    ctx: ToolContext, agent_name: str, content: str, wait: bool,
) -> str:
    """Reach the AgentManager via the loopback internal API.

    Tools run inside the MCP server — a subprocess with no handle on the
    AgentManager. The engine passes the loopback address and a per-boot
    bearer token via env (see src/core/internal_api.py); without them,
    inter-agent communication is genuinely unavailable.
    """
    api = os.environ.get("KBOTS_INTERNAL_API", "")
    token = os.environ.get("KBOTS_INTERNAL_TOKEN", "")
    if not api or not token:
        return "No agent manager available — inter-agent communication not supported."

    depth = _env_depth()
    if depth >= MAX_INTER_AGENT_DEPTH:
        return (
            f"Inter-agent call depth limit reached ({MAX_INTER_AGENT_DEPTH}). "
            f"Cannot reach {agent_name} — this prevents infinite agent loops."
        )
    if agent_name == ctx.agent_id:
        return "Cannot message yourself — use a different agent."

    import aiohttp

    # An ask waits for the target agent's full turn (minutes); a send only
    # needs the dispatch acknowledged.
    timeout = aiohttp.ClientTimeout(total=900 if wait else 30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{api}/agent-message",
                json={"target": agent_name, "content": content,
                      "from_agent": ctx.agent_id, "depth": depth, "wait": wait},
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    if "available" in data:
                        return (f"Unknown agent '{agent_name}'. "
                                f"Available agents: {data['available']}")
                    return f"Inter-agent call failed: {data.get('error', f'HTTP {resp.status}')}"
                if wait:
                    return data.get("response") or f"Agent {agent_name} returned no response."
                return _format_send_result(
                    agent_name, data.get("delivery"), data.get("channel_id"))
    except Exception as e:
        return f"Error communicating with {agent_name}: {e}"


def _format_send_result(agent_name: str, delivery: str | None,
                        channel_id: str | None) -> str:
    """Honest fire-and-forget result: say where the message went and that no
    reply comes back to the sender — 'dispatched' alone reads as delivered-
    and-seen when the target's response would otherwise never surface."""
    if delivery == "channel" and channel_id:
        return (f"Message delivered to {agent_name}'s channel <#{channel_id}> — "
                f"they will process it with full context and their reply is "
                f"posted in that channel, NOT returned to you. Use ask_agent "
                f"if you need the answer.")
    return (f"Message dispatched to {agent_name} in a background session. "
            f"Their response is not returned to you and is not posted "
            f"anywhere — use ask_agent if the outcome matters.")


def _build_inter_agent_message(
    ctx: ToolContext, target_agent: str, content: str,
) -> IncomingMessage:
    """Build an IncomingMessage for inter-agent communication."""
    msg = IncomingMessage(
        connector="internal",
        channel_id=f"internal:{ctx.agent_id}:{target_agent}",
        user_id=ctx.agent_id,
        user_name=f"agent:{ctx.agent_id}",
        content=content,
    )
    # Carry depth through the message so the target agent's ToolContext inherits it
    msg._inter_agent_depth = ctx.inter_agent_depth + 1  # type: ignore[attr-defined]
    return msg


@tool(name="send_message", description="Send a message to a Discord channel or user")
async def send_message(ctx: ToolContext, channel_id: str, content: str, bot: str = "") -> str:
    """Send a message to a specific channel.

    Args:
        channel_id: The Discord channel ID to send to.
        content: The message content.
        bot: Which bot account to send as (e.g. 'main', 'assistant'). Leave empty for default.
    """
    if ctx.connector_send and not bot:
        await ctx.connector_send(channel_id, content)
        return f"Message sent to {channel_id}"

    # Fallback: send via Discord API directly (MCP server context)
    if ctx.vault:
        token = None
        if bot:
            token = ctx.vault.get(f"discord-token-{bot}")
            if not token:
                return f"Error: no Discord token for bot '{bot}'."
        else:
            token = ctx.vault.get("active-discord-token") or ctx.vault.get("discord-token")
        if token:
            import aiohttp
            headers = {
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com/karvidsson/kbots, 1.0)",
            }
            # Split long messages (Discord 2000 char limit)
            chunks = [content[i:i+2000] for i in range(0, len(content), 2000)]
            async with aiohttp.ClientSession() as session:
                for chunk in chunks:
                    async with session.post(
                        f"https://discord.com/api/v10/channels/{channel_id}/messages",
                        headers=headers,
                        json={"content": chunk},
                    ) as resp:
                        if resp.status != 200:
                            err = await resp.text()
                            return f"Discord API error ({resp.status}): {err[:200]}"
            return f"Message sent to {channel_id}"

    return "No connector available to send messages"


@tool(name="ask_agent", description="Ask another agent a question and wait for their response")
async def ask_agent(ctx: ToolContext, agent_name: str, question: str) -> str:
    """Ask another agent a question. Returns their response.

    This is a synchronous call — waits for the other agent to respond.
    Use send_to_agent for fire-and-forget.
    """
    if not ctx.agent_manager:
        # MCP subprocess — route through the loopback API in the main process.
        return await _loopback_agent_call(ctx, agent_name, question, wait=True)

    # Depth check to prevent infinite loops
    if ctx.inter_agent_depth >= MAX_INTER_AGENT_DEPTH:
        return (
            f"Inter-agent call depth limit reached ({MAX_INTER_AGENT_DEPTH}). "
            f"Cannot ask {agent_name} — this prevents infinite agent loops."
        )

    # Verify target agent exists
    if agent_name not in ctx.agent_manager.agent_configs:
        available = list(ctx.agent_manager.agent_configs.keys())
        return f"Unknown agent '{agent_name}'. Available agents: {available}"

    # Prevent self-calls
    if agent_name == ctx.agent_id:
        return "Cannot ask yourself — use a different agent."

    logger.info(
        f"Inter-agent ask: {ctx.agent_id} -> {agent_name} "
        f"(depth {ctx.inter_agent_depth + 1}/{MAX_INTER_AGENT_DEPTH})"
    )

    msg = _build_inter_agent_message(ctx, agent_name, question)

    try:
        response = await ctx.agent_manager.handle_internal_message(agent_name, msg)
        if response:
            return response
        return f"Agent {agent_name} returned no response."
    except Exception as e:
        logger.error(f"Inter-agent ask failed ({ctx.agent_id} -> {agent_name}): {e}", exc_info=True)
        return f"Error communicating with {agent_name}: {e}"


@tool(name="send_to_agent",
      description="Send a message to another agent (fire and forget). It is "
                  "delivered into their home channel; their reply is posted "
                  "there, not returned to you — use ask_agent for an answer")
async def send_to_agent(ctx: ToolContext, agent_name: str, message: str) -> str:
    """Send a message to another agent without waiting for a response.

    Fire-and-forget — delivered into the target's home channel so their reply
    stays visible there; nothing is returned to the sender.
    """
    if not ctx.agent_manager:
        # MCP subprocess — route through the loopback API in the main process.
        return await _loopback_agent_call(ctx, agent_name, message, wait=False)

    # Depth check
    if ctx.inter_agent_depth >= MAX_INTER_AGENT_DEPTH:
        return (
            f"Inter-agent call depth limit reached ({MAX_INTER_AGENT_DEPTH}). "
            f"Cannot send to {agent_name} — this prevents infinite agent loops."
        )

    # Verify target agent exists
    if agent_name not in ctx.agent_manager.agent_configs:
        available = list(ctx.agent_manager.agent_configs.keys())
        return f"Unknown agent '{agent_name}'. Available agents: {available}"

    # Prevent self-sends
    if agent_name == ctx.agent_id:
        return "Cannot send to yourself — use a different agent."

    logger.info(
        f"Inter-agent send: {ctx.agent_id} -> {agent_name} "
        f"(depth {ctx.inter_agent_depth + 1}/{MAX_INTER_AGENT_DEPTH})"
    )

    result = await ctx.agent_manager.deliver_inter_agent_message(
        agent_name, ctx.agent_id, message, ctx.inter_agent_depth + 1)
    return _format_send_result(
        agent_name, result.get("delivery"), result.get("channel_id"))
