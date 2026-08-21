"""discord_set_bot_name and discord_send_dm.

Two things these pin deliberately: a refused rename is never retried (Discord
allows two username changes per hour, and a doomed request still spends one),
and a DM is never posted when the channel could not be opened.
"""

from unittest.mock import MagicMock, patch

from src.core.base import ToolContext
from src.tools.discord_tools import discord_send_dm, discord_set_bot_name

HEADERS = {"Authorization": "Bot t"}


class _FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status = status
        self._payload = payload if payload is not None else {}
        self._text = text

    async def json(self):
        return self._payload

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Routes by (method, url suffix) and records every request it was given."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def patch(self, url, **kw):
        return self._route("PATCH", url, kw)

    def post(self, url, **kw):
        return self._route("POST", url, kw)

    def _route(self, method, url, kw):
        self.calls.append((method, url, kw.get("json")))
        for (m, suffix), resp in self.routes.items():
            if m == method and url.endswith(suffix):
                return resp
        return _FakeResp(500, text=f"unrouted {method} {url}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _ctx():
    return ToolContext(agent_id="botson", vault=MagicMock())


async def _run(session, coro_factory):
    # Await INSIDE the patch. Returning the un-awaited coroutine would let the
    # patch unwind first and the tool would reach the real Discord API.
    with patch("src.tools.discord_tools._discord_headers", return_value=HEADERS), \
         patch("aiohttp.ClientSession", return_value=session):
        return await coro_factory()


# --- discord_set_bot_name ---------------------------------------------------

async def test_a_rename_reports_the_name_discord_confirmed():
    session = _FakeSession({("PATCH", "/users/@me"): _FakeResp(
        200, {"id": "1", "username": "Botson"})})
    out = await _run(session, lambda: discord_set_bot_name(_ctx(), "Botson", bot="botson"))
    assert out == "Bot account renamed to 'Botson'."
    assert len(session.calls) == 1
    assert session.calls[0][2] == {"username": "Botson"}


async def test_a_name_that_cannot_be_valid_is_never_sent():
    """A rejected request still spends one of the two changes allowed per hour."""
    session = _FakeSession({})
    out = await _run(session, lambda: discord_set_bot_name(_ctx(), "x", bot="botson"))
    assert "Not sent" in out
    assert session.calls == []


async def test_an_over_long_name_is_never_sent():
    session = _FakeSession({})
    out = await _run(session, lambda: discord_set_bot_name(_ctx(), "b" * 33, bot="botson"))
    assert "Not sent" in out
    assert session.calls == []


async def test_a_rate_limit_is_reported_and_not_retried():
    session = _FakeSession({("PATCH", "/users/@me"): _FakeResp(
        429, text='{"retry_after": 1800}')})
    out = await _run(session, lambda: discord_set_bot_name(_ctx(), "Botson", bot="botson"))
    assert "Rate limited" in out and "NOT retried" in out
    assert len(session.calls) == 1, "a retry would burn the second change of the hour"


async def test_a_rejected_name_says_so_rather_than_failing_generically():
    session = _FakeSession({("PATCH", "/users/@me"): _FakeResp(
        400, text='{"username": ["contains disallowed characters"]}')})
    out = await _run(session, lambda: discord_set_bot_name(_ctx(), "Bot.Son", bot="botson"))
    assert "rejected the name 'Bot.Son'" in out
    assert "disallowed" in out


async def test_a_missing_token_is_named_not_swallowed():
    with patch("src.tools.discord_tools._discord_headers", return_value=None):
        out = await discord_set_bot_name(_ctx(), "Botson", bot="botson")
    assert "No Discord token found for bot 'botson'" in out


# --- discord_send_dm --------------------------------------------------------

async def test_a_dm_opens_the_channel_then_posts():
    session = _FakeSession({
        ("POST", "/users/@me/channels"): _FakeResp(200, {"id": "dm1"}),
        ("POST", "/channels/dm1/messages"): _FakeResp(200, {"id": "m1"}),
    })
    out = await _run(session, lambda: discord_send_dm(_ctx(), "999", "hi", bot="botson"))
    assert out == "DM sent to 999 (channel dm1)."
    assert [c[0] for c in session.calls] == ["POST", "POST"]
    assert session.calls[1][2] == {"content": "hi"}


async def test_a_closed_dm_reads_as_a_privacy_setting_not_a_bug():
    session = _FakeSession({
        ("POST", "/users/@me/channels"): _FakeResp(403, text="Cannot send messages to this user"),
    })
    out = await _run(session, lambda: discord_send_dm(_ctx(), "999", "hi", bot="botson"))
    assert "do not accept direct messages" in out
    assert len(session.calls) == 1, "no message may be posted to a channel that never opened"


async def test_a_failed_channel_open_never_attempts_a_post():
    session = _FakeSession({
        ("POST", "/users/@me/channels"): _FakeResp(500, text="server error"),
    })
    out = await _run(session, lambda: discord_send_dm(_ctx(), "999", "hi", bot="botson"))
    assert "Could not open a DM channel" in out
    assert len(session.calls) == 1


async def test_a_refused_post_is_distinguished_from_a_refused_open():
    session = _FakeSession({
        ("POST", "/users/@me/channels"): _FakeResp(200, {"id": "dm1"}),
        ("POST", "/channels/dm1/messages"): _FakeResp(403, text="forbidden"),
    })
    out = await _run(session, lambda: discord_send_dm(_ctx(), "999", "hi", bot="botson"))
    assert "opened, but posting was refused" in out


async def test_an_over_long_message_is_never_sent():
    session = _FakeSession({})
    out = await _run(session, lambda: discord_send_dm(_ctx(), "999", "x" * 2001, bot="botson"))
    assert "2000 characters" in out
    assert session.calls == []


async def test_an_empty_recipient_or_body_is_refused_before_any_call():
    session = _FakeSession({})
    assert "user_id is required" in await _run(
        session, lambda: discord_send_dm(_ctx(), "", "hi"))
    assert "content is empty" in await _run(
        session, lambda: discord_send_dm(_ctx(), "999", "   "))
    assert session.calls == []
