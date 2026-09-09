"""Reading emoji verdicts off Discord messages.

An approval loop where the owner marks a post with an emoji instead of
replying was unreadable: reactions never reached the agent. on_raw_reaction_add
routes a fixed set (HITL ✅/❌, lesson 👍/👎, the shortener and reveal emoji,
schedule ❌) and drops everything else, so no wake-up arrives, and
read_channel_history rendered author, text and attachments only. From the
agent's side an approved post and an ignored post looked identical.
"""

from unittest.mock import MagicMock, patch

from src.core.base import ToolContext
from src.tools.discord_tools import _format_message, discord_reactions

MSG_WITH_REACTIONS = {
    "id": "m1",
    "author": {"username": "atlas"},
    "timestamp": "2026-09-09T08:00:00+00:00",
    "content": "clip 12",
    "reactions": [
        {"emoji": {"id": None, "name": "✅"}, "count": 1},
        {"emoji": {"id": None, "name": "🔴"}, "count": 2},
    ],
}


def _ctx():
    return ToolContext(agent_id="atlas", vault=MagicMock())


def _get(routes):
    async def get(vault, endpoint, bot=""):
        for suffix, value in routes.items():
            if endpoint.endswith(suffix):
                return value
        return None
    return get


# --- counts, from the field Discord already sends -------------------------

def test_history_shows_reaction_counts():
    out = _format_message(MSG_WITH_REACTIONS)
    assert "✅x1" in out
    assert "🔴x2" in out


def test_history_is_unchanged_for_a_message_with_no_reactions():
    out = _format_message({k: v for k, v in MSG_WITH_REACTIONS.items()
                           if k != "reactions"})
    assert "Reactions" not in out
    assert "clip 12" in out


def test_history_survives_a_reaction_with_no_emoji_name():
    """Deleted custom emoji come back with name None; a verdict scan must not
    crash on someone else's tidying-up."""
    out = _format_message({**MSG_WITH_REACTIONS,
                           "reactions": [{"emoji": {"name": None}, "count": 1}]})
    assert "Reactions" in out


# --- identity, which is what makes a verdict a verdict --------------------

async def test_lists_who_reacted_with_each_emoji():
    get = _get({
        "/messages/m1": MSG_WITH_REACTIONS,
        "/reactions/%E2%9C%85?limit=100": [{"id": "1", "username": "owner"}],
        "/reactions/%F0%9F%94%B4?limit=100": [
            {"id": "2", "username": "atlas"}, {"id": "3", "username": "data-bot"}],
    })
    with patch("src.tools.discord_tools._discord_get", side_effect=get):
        out = await discord_reactions(_ctx(), "c1", "m1")
    assert "owner (1)" in out
    assert "atlas (2)" in out and "data-bot (3)" in out


async def test_user_ids_are_present_so_a_peer_cannot_pass_as_the_owner():
    """The reason counts alone are not enough: every agent in the channel can
    add the same emoji, and acting on the count would let a peer approve its
    own work."""
    get = _get({
        "/messages/m1": {**MSG_WITH_REACTIONS, "reactions": [
            {"emoji": {"id": None, "name": "✅"}, "count": 1}]},
        "/reactions/%E2%9C%85?limit=100": [{"id": "999", "username": "data-bot"}],
    })
    with patch("src.tools.discord_tools._discord_get", side_effect=get):
        out = await discord_reactions(_ctx(), "c1", "m1")
    assert "999" in out


async def test_custom_emoji_are_addressed_by_name_and_id():
    """Discord's reactions endpoint needs 'name:id' for custom emoji; sending
    the name alone 404s and reads as 'nobody reacted'."""
    seen = []

    async def get(vault, endpoint, bot=""):
        seen.append(endpoint)
        if endpoint.endswith("/messages/m1"):
            return {**MSG_WITH_REACTIONS, "reactions": [
                {"emoji": {"id": "42", "name": "approve"}, "count": 1}]}
        return [{"id": "1", "username": "owner"}]

    with patch("src.tools.discord_tools._discord_get", side_effect=get):
        await discord_reactions(_ctx(), "c1", "m1")
    assert any("approve%3A42" in e for e in seen), seen


async def test_no_reactions_is_stated_not_faked():
    get = _get({"/messages/m1": {**MSG_WITH_REACTIONS, "reactions": []}})
    with patch("src.tools.discord_tools._discord_get", side_effect=get):
        out = await discord_reactions(_ctx(), "c1", "m1")
    assert "no reactions" in out.lower()


async def test_unreadable_reactor_list_says_so_rather_than_claiming_nobody():
    """Silently reporting an empty list would read as 'not approved yet' and
    strand the post."""
    async def get(vault, endpoint, bot=""):
        return MSG_WITH_REACTIONS if endpoint.endswith("/messages/m1") else None

    with patch("src.tools.discord_tools._discord_get", side_effect=get):
        out = await discord_reactions(_ctx(), "c1", "m1")
    assert "could not read who reacted" in out


async def test_missing_message_errors_cleanly():
    with patch("src.tools.discord_tools._discord_get", side_effect=_get({})):
        out = await discord_reactions(_ctx(), "c1", "gone")
    assert out.startswith("Error")


async def test_no_vault_errors_cleanly():
    ctx = ToolContext(agent_id="atlas", vault=None)
    assert (await discord_reactions(ctx, "c1", "m1")).startswith("Error")


# --- the id of the message we just posted ---------------------------------
#
# send_discord_file discarded Discord's response, so a caller that needed to
# watch "the post I just made" for a verdict had to guess by timestamp. That
# picks the wrong message as soon as two posts land close together.

class _FakeResp:
    status = 200

    async def json(self):
        return {"id": "m-new", "channel_id": "c1"}

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, *a, **k):
        pass

    def post(self, *a, **k):
        return _FakeResp()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def test_send_discord_file_returns_the_new_message_id(tmp_path, monkeypatch):
    from src.tools import discord_tools

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr(discord_tools, "_discord_headers",
                        lambda *a, **k: {"Authorization": "Bot t"})
    monkeypatch.setattr(discord_tools.aiohttp, "ClientSession", _FakeSession)
    # Imported inside send_discord_file, so it is patched on its own module.
    monkeypatch.setattr("src.tools.ingest.validate_file_path",
                        lambda *a, **k: None)

    out = await discord_tools.send_discord_file(
        _ctx(), "c1", str(clip), message="clip 12")
    assert "m-new" in out
