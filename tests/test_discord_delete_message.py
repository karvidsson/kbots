"""discord_delete_message — a bot may delete its own messages and nothing else."""

from unittest.mock import MagicMock, patch

from src.core.base import ToolContext
from src.tools.discord_tools import discord_delete_message

BOT_USER = {"id": "111", "username": "pixel-fox"}
OWN_MSG = {"id": "m1", "author": {"id": "111", "username": "pixel-fox"}}
OTHER_MSG = {"id": "m2", "author": {"id": "222", "username": "alice"}}


def _ctx():
    return ToolContext(agent_id="pixel-fox", vault=MagicMock())


async def _fake_get(routes):
    async def get(vault, endpoint, bot=""):
        for suffix, value in routes.items():
            if endpoint.endswith(suffix):
                return value
        return None
    return get


async def test_deletes_own_message():
    get = await _fake_get({"/users/@me": BOT_USER, "/messages/m1": OWN_MSG})
    with patch("src.tools.discord_tools._discord_get", side_effect=get), \
         patch("src.tools.discord_tools._discord_delete") as mock_del:
        mock_del.return_value = {"success": True}
        out = await discord_delete_message(_ctx(), "c1", "m1", bot="pixel-fox")
    assert "Deleted message m1" in out
    mock_del.assert_called_once()


async def test_refuses_someone_elses_message():
    get = await _fake_get({"/users/@me": BOT_USER, "/messages/m2": OTHER_MSG})
    with patch("src.tools.discord_tools._discord_get", side_effect=get), \
         patch("src.tools.discord_tools._discord_delete") as mock_del:
        out = await discord_delete_message(_ctx(), "c1", "m2", bot="pixel-fox")
    assert out.startswith("Refused")
    mock_del.assert_not_called()


async def test_no_vault_errors_cleanly():
    ctx = ToolContext(agent_id="pixel-fox", vault=None)
    out = await discord_delete_message(ctx, "c1", "m1")
    assert out.startswith("Error")


def test_format_message_includes_id():
    from src.tools.discord_tools import _format_message
    msg = {"id": "1534567890", "author": {"username": "pixel-fox"},
           "content": "hello", "timestamp": "2026-08-05T08:10:00Z"}
    out = _format_message(msg)
    assert "(id 1534567890)" in out
    assert "pixel-fox" in out and "hello" in out
