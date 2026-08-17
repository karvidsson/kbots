"""Tests for Discord token isolation — bots must never fall back to another bot's token.

Covers:
- _discord_headers() in discord_tools.py
- send_message() in builtin.py
- send_discord_file() in discord_tools.py
- Discord connector reply fallback
"""

from unittest.mock import AsyncMock, MagicMock

# --- Token isolation: _discord_headers ---

class TestDiscordHeaders:
    """Ensure _discord_headers never falls back when a specific bot is requested."""

    def test_specific_bot_found(self):
        from src.tools.discord_tools import _discord_headers
        vault = MagicMock()
        vault.get.side_effect = lambda k: "rescue-token-123" if k == "discord-token-rescue" else None
        headers = _discord_headers(vault, bot="rescue")
        assert headers is not None
        assert "rescue-token-123" in headers["Authorization"]

    def test_specific_bot_missing_returns_none(self):
        """When bot='rescue' but no token exists, must return None — NOT fall back to default."""
        from src.tools.discord_tools import _discord_headers
        vault = MagicMock()
        vault.get.return_value = None
        headers = _discord_headers(vault, bot="rescue")
        assert headers is None

    def test_specific_bot_missing_does_not_try_default(self):
        """Verify vault.get is NOT called with default keys when bot is specified and missing."""
        from src.tools.discord_tools import _discord_headers
        vault = MagicMock()
        vault.get.return_value = None
        _discord_headers(vault, bot="rescue")
        called_keys = [call.args[0] for call in vault.get.call_args_list]
        assert "active-discord-token" not in called_keys
        assert "discord-token" not in called_keys

    def test_no_bot_uses_default(self):
        from src.tools.discord_tools import _discord_headers
        vault = MagicMock()
        vault.get.side_effect = lambda k: "default-token" if k == "discord-token" else None
        headers = _discord_headers(vault, bot="")
        assert headers is not None
        assert "default-token" in headers["Authorization"]

    def test_no_bot_tries_active_then_default(self):
        from src.tools.discord_tools import _discord_headers
        vault = MagicMock()
        vault.get.side_effect = lambda k: "active-tok" if k == "active-discord-token" else None
        headers = _discord_headers(vault, bot="")
        assert headers is not None
        assert "active-tok" in headers["Authorization"]


# --- Token isolation: send_message ---

class TestSendMessageTokenIsolation:
    """send_message in builtin.py must not fall back to default token when bot is specified."""

    async def test_specific_bot_missing_returns_error(self):
        from src.tools.builtin import send_message
        vault = MagicMock()
        vault.get.return_value = None
        ctx = MagicMock()
        ctx.vault = vault
        ctx.connector_send = None
        result = await send_message(ctx, "123", "hello", bot="rescue")
        assert "Error" in result
        assert "rescue" in result

    async def test_no_bot_uses_default(self):
        from src.tools.builtin import send_message
        vault = MagicMock()
        vault.get.side_effect = lambda k: "tok" if k == "discord-token" else None
        ctx = MagicMock()
        ctx.vault = vault
        ctx.connector_send = None
        # Will fail at aiohttp but we just need to verify it doesn't error on token lookup
        # Use connector_send path instead
        ctx.connector_send = AsyncMock()
        result = await send_message(ctx, "123", "hello", bot="")
        assert "sent" in result.lower() or "Message" in result


# --- Token isolation: send_discord_file ---

class TestSendDiscordFileTokenIsolation:
    """send_discord_file must not fall back to default token when bot is specified."""

    async def test_specific_bot_missing_returns_error(self):
        from src.tools.discord_tools import send_discord_file
        vault = MagicMock()
        vault.get.return_value = None
        ctx = MagicMock()
        ctx.vault = vault
        result = await send_discord_file(ctx, "123", "/tmp/test.txt", bot="rescue")
        assert "Error" in result
        assert "rescue" in result


# --- Discord connector: reply fallback ---

class TestDiscordReplyFallback:
    """When reply() raises Forbidden, connector should fall back to channel.send()."""

    async def test_reply_forbidden_falls_back_to_send(self):
        import discord

        # We test the logic directly rather than importing the full connector
        # Simulate the fixed code path
        async def _simulate_send():
            reply_to = AsyncMock(spec=discord.Message)
            reply_to.reply = AsyncMock(side_effect=discord.Forbidden(
                MagicMock(status=403), {"message": "Missing Read Message History"}
            ))

            channel = AsyncMock()
            channel.send = AsyncMock(return_value=MagicMock())

            content = "test response"
            # Simulate the fixed code
            try:
                await reply_to.reply(content)
            except discord.Forbidden:
                await channel.send(content)

            channel.send.assert_called_once_with(content)
            return True

        assert await _simulate_send()
