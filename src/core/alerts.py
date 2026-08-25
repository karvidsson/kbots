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


async def send_alert(message: str, channel_id: str, bot_token: str) -> bool:
    """Send an alert message to a Discord channel.

    Args:
        message: Alert text (truncated to 2000 chars)
        channel_id: Discord channel ID
        bot_token: Discord bot token for authentication

    Returns True if sent successfully, False otherwise.
    """
    if not channel_id or not bot_token:
        logger.warning(f"Security alert (no channel configured): {message}")
        return False

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {bot_token}"},
                json={"content": message[:2000]},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 201):
                    return True
                logger.warning(f"Alert send failed (HTTP {resp.status})")
                return False
    except Exception as e:
        logger.warning(f"Alert send failed: {e}")
        return False


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

    async def send(self, message: str) -> bool:
        """Send an alert. Fire-and-forget safe."""
        if not self.enabled:
            logger.warning(f"Security alert (no channel): {message}")
            return False
        return await send_alert(message, self.channel_id, self.bot_token)

    def send_bg(self, message: str) -> None:
        """Send an alert in the background — doesn't block the caller."""
        if not self.enabled:
            logger.warning(f"Security alert (no channel): {message}")
            return
        asyncio.create_task(
            send_alert(message, self.channel_id, self.bot_token),
            name="security-alert",
        )

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
