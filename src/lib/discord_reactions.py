"""Best-effort reaction shortcuts on an already delivered REST message."""

import logging
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)


async def seed_reactions_rest(session, headers: dict, channel_id: str, message_id: str,
                              emojis: tuple[str, ...]) -> list[str]:
    """Return failed shortcuts without resending or re-resolving bot identity."""
    failed = []
    for emoji in emojis:
        if not message_id:
            failed.append(emoji)
            continue
        endpoint = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
        try:
            async with session.put(
                f"{endpoint}/reactions/{quote(emoji, safe='')}/@me",
                headers=headers, timeout=aiohttp.ClientTimeout(total=5),
            ) as reaction:
                if reaction.status not in (200, 204):
                    failed.append(emoji)
                    logger.warning("decision-reactions: HTTP %s on message %s", reaction.status, message_id)
        except (aiohttp.ClientError, OSError, TimeoutError):
            failed.append(emoji)
            logger.warning("decision-reactions: request failed on message %s", message_id)
    return failed
