"""Security alerts — send to configured Discord channel.

Reads alert_channel and alert_bot from security config (Layer 3).
Falls back to logger if no channel configured.
"""

import asyncio
import logging

import aiohttp

from src.core import runtime_state

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"

# Tools whose output should be scanned for injection content
WEB_FACING_TOOLS = frozenset({
    "web_search", "browse_url", "read_url", "browser",
    "gmail_read", "gmail_search", "download_file",
})

#: Which argument names a web-facing tool's target, best first. An alert that
#: names only the tool cannot be acted on: "read_url stripped 1,656 chars" does
#: not say whether that was a news site or somewhere nothing should have been
#: fetching from, and those need opposite responses.
_TARGET_ARGS = ("url", "query", "target", "q", "search", "link", "address")


def describe_target(tool_name: str, kwargs: dict, limit: int = 120) -> str:
    """The thing a web-facing tool was pointed at, for an alert line.

    Argument names differ per tool (`url` for read_url, `query` for web_search,
    and `browser` takes whichever of several it is driving), so this reads the
    call rather than hard-coding a table that a new tool would silently fall out
    of. Falls back to the first string argument, because a tool that names its
    target something unforeseen should still be attributable.
    """
    if not kwargs:
        return ""
    for name in _TARGET_ARGS:
        value = kwargs.get(name)
        if isinstance(value, str) and value.strip():
            return _clip(value.strip(), limit)
    for value in kwargs.values():
        if isinstance(value, str) and value.strip():
            return _clip(value.strip(), limit)
    return ""


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def send_alert(message: str, channel_id: str, bot_token: str) -> str | None:
    """Send an alert message to a Discord channel.

    Args:
        message: Alert text (truncated to 2000 chars)
        channel_id: Discord channel ID
        bot_token: Discord bot token for authentication

    Returns the id of the posted message, or None if it was not sent. The id
    rather than a bare bool because a reaction handler needs something to key
    a follow-up against, and this is the only place the created message is
    visible; the response used to be discarded, so an alert could not be
    referred to after it was posted.
    """
    if not channel_id or not bot_token:
        logger.warning(f"Security alert (no channel configured): {message}")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {bot_token}"},
                json={"content": message[:2000]},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 201):
                    try:
                        body = await resp.json()
                        return str(body.get("id") or "") or None
                    except Exception:
                        # Posted, but the id could not be read. The alert is
                        # what matters; a missing id costs only the follow-up.
                        return None
                logger.warning(f"Alert send failed (HTTP {resp.status})")
                return None
    except Exception as e:
        logger.warning(f"Alert send failed: {e}")
        return None


class AlertSender:
    """Alert sender — token resolved once, channel resolved per send."""

    def __init__(self, config: dict, vault=None):
        security = config.get("security", {})
        self._config_channel = security.get("alert_channel", "")
        alert_bot = security.get("alert_bot", "")
        self.bot_token = ""
        if alert_bot and vault:
            # Resolve via the connector's account→token_key map so alert_bot can
            # name a bot account (e.g. "main" → discord-token). Fall back to the
            # discord-<name> convention for any bot not in accounts.
            accounts = config.get("connectors", {}).get("discord", {}).get("accounts", {})
            token_key = accounts.get(alert_bot, {}).get("token_key", f"discord-{alert_bot}")
            self.bot_token = vault.get(token_key) or ""

    @property
    def channel_id(self) -> str:
        """Alert channel — a runtime override wins over config, so server setup
        can wire this live rather than needing a config edit and a restart.
        Resolved per send, not cached at boot: caching is what made this the one
        channel role of four that could not be provisioned without downtime."""
        return runtime_state.get_flag("alert_channel", None) or self._config_channel

    @property
    def enabled(self) -> bool:
        return bool(self.channel_id and self.bot_token)

    async def send(self, message: str) -> str | None:
        """Send an alert. Returns the posted message id, or None."""
        if not self.enabled:
            logger.warning(f"Security alert (no channel): {message}")
            return None
        return await send_alert(message, self.channel_id, self.bot_token)

    def send_bg(self, message: str, on_sent=None) -> None:
        """Send an alert in the background — doesn't block the caller.

        `on_sent` is called with the posted message id once it exists. It is a
        callback rather than a return value because the caller here is a
        synchronous tool wrapper that must not wait on Discord; anything keyed
        to the alert's message id has to be stored after the fact.
        """
        if not self.enabled:
            logger.warning(f"Security alert (no channel): {message}")
            return

        async def _send() -> None:
            message_id = await send_alert(message, self.channel_id, self.bot_token)
            if message_id and on_sent:
                try:
                    on_sent(message_id)
                except Exception as e:
                    logger.warning(f"Alert follow-up store failed: {e}")

        asyncio.create_task(_send(), name="security-alert")

    def post_bg(self, channel_id: str, message: str) -> None:
        """Post to a specific channel with the alert bot's token, falling back
        to the alert channel. Used by the platform-update notice, which has its
        own channel but no bot of its own."""
        target = str(channel_id or "") or self.channel_id
        if not (target and self.bot_token):
            logger.warning(f"Platform notice (no channel): {message}")
            return
        asyncio.create_task(
            send_alert(message, target, self.bot_token), name="platform-notice")
