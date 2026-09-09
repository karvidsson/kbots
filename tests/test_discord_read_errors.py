"""A failed Discord read has to say which failure it was.

_discord_get collapsed every non-200 into None, so the read tools could only
answer "could not fetch messages ... Check the channel ID and bot permissions".
That one sentence covers a wrong id, a bot that is not in the channel, and a
rate limit, and distinguishes none of them. The status WAS logged, but to the
MCP server's own log, which the agent holding the failed result cannot read.

The identity in the message is the load-bearing part: a read authenticates as
the calling agent's own bot, so the same channel legitimately reads for one
agent and 404s for another. Without the bot name in the error that looks like
a broken tool instead of a membership fact.
"""

from unittest.mock import MagicMock

import pytest

import src.lib.discord_auth as da
from src.core.base import ToolContext
from src.tools import discord_tools
from src.tools.discord_tools import read_channel_history, read_message


class _Resp:
    def __init__(self, status, body=""):
        self.status = status
        self._body = body

    async def json(self):
        return []

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    status, body = 403, '{"message": "Missing Access", "code": 50001}'

    def __init__(self, *a, **k):
        pass

    def get(self, *a, **k):
        return _Resp(self.status, self.body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def api(monkeypatch):
    """A Discord that answers with a configurable status, as a known bot."""
    monkeypatch.setattr(
        discord_tools, "resolve_bot_token",
        lambda vault, bot="", agent_id="": da.BotAuth("tok", "atlas", None))

    def _with(status, body=""):
        cls = type("S", (_Session,), {"status": status, "body": body})
        monkeypatch.setattr(discord_tools.aiohttp, "ClientSession", cls)
    return _with


def _ctx():
    return ToolContext(agent_id="atlas", vault=MagicMock())


async def test_permission_failure_names_the_status_and_the_bot(api):
    api(403, '{"message": "Missing Access", "code": 50001}')
    out = await read_channel_history(_ctx(), "c1")
    assert "403" in out
    assert "atlas" in out
    assert "Missing Access" in out


async def test_404_says_a_private_channel_reads_as_not_found(api):
    """The trap: a channel the bot is simply not in returns 404, not 403, so
    "does not exist" is the wrong first conclusion."""
    api(404, '{"message": "Unknown Channel", "code": 10003}')
    out = await read_channel_history(_ctx(), "c1")
    assert "404" in out
    assert "cannot see it" in out


async def test_rate_limit_is_not_reported_as_a_permission_problem(api):
    api(429, '{"retry_after": 4.2}')
    out = await read_channel_history(_ctx(), "c1")
    assert "429" in out
    assert "rate limited" in out.lower()
    assert "permission" not in out.lower()


async def test_read_message_carries_the_same_detail(api):
    api(403, '{"message": "Missing Access"}')
    out = await read_message(_ctx(), "c1", "m1")
    assert "403" in out and "atlas" in out


async def test_an_unmapped_status_still_returns_discords_own_message(api):
    """No invented hint for statuses we have no advice for; the API's text is
    better than a guess."""
    api(500, '{"message": "Internal Server Error"}')
    out = await read_channel_history(_ctx(), "c1")
    assert "500" in out
    assert "Internal Server Error" in out


async def test_a_missing_token_is_reported_rather_than_looking_like_a_404(
        monkeypatch):
    """No token is a config problem, not a permission or id problem, and
    saying "check the channel ID" sends the reader down the wrong path."""
    monkeypatch.setattr(
        discord_tools, "resolve_bot_token",
        lambda vault, bot="", agent_id="": da.BotAuth(
            None, "atlas", "Error: no Discord token for bot 'atlas'"))
    out = await read_channel_history(_ctx(), "c1")
    assert "no Discord token" in out


async def test_success_is_unchanged(api, monkeypatch):
    """The happy path must not acquire an error suffix."""
    class OK(_Session):
        status = 200

        def get(self, *a, **k):
            class R(_Resp):
                async def json(self):
                    return [{"id": "m1", "author": {"username": "atlas"},
                             "content": "hi", "timestamp": ""}]
            return R(200)

    monkeypatch.setattr(discord_tools.aiohttp, "ClientSession", OK)
    out = await read_channel_history(_ctx(), "c1")
    assert "Error" not in out
    assert "hi" in out
