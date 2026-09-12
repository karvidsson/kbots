"""An unhandled reaction must reach the bot it was aimed at.

Reactions reached an agent only through a fixed set of handlers (HITL ✅/❌,
lesson 👍/👎, the shortener and reveal emoji, schedule ❌). Everything else
returned with nothing logged, so an owner marking a post looked, from the
agent's side, exactly like being ignored.

The two guards below are the whole design. on_raw_reaction_add runs on EVERY
gateway client in the process, so without the author check a single reaction
wakes every bot on the fleet; and agents react to each other, so without the
bot check the fleet talks to itself at one turn per reaction.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.connectors.discord import DiscordBot

OUR_BOT = 111
OTHER_BOT = 222
HUMAN = 999


def _payload(user_id=HUMAN, member_bot=False, member=True):
    m = SimpleNamespace(bot=member_bot, display_name="owner") if member else None
    return SimpleNamespace(user_id=user_id, channel_id=5, message_id=7,
                           emoji="🔴", member=m)


def _handler(author_id=OUR_BOT, reaction_wake=True, fetch_raises=None):
    """A DiscordBot wired with just enough to run _wake_on_reaction."""
    h = DiscordBot.__new__(DiscordBot)
    h.account_name = "atlas"
    h.client = SimpleNamespace(user=SimpleNamespace(id=OUR_BOT),
                               get_user=lambda _id: SimpleNamespace(
                                   bot=False, display_name="owner"),
                               fetch_user=AsyncMock())
    message = SimpleNamespace(author=SimpleNamespace(id=author_id),
                              content="clip 12")
    channel = SimpleNamespace(name="studio",
                              fetch_message=AsyncMock(return_value=message))
    if fetch_raises is not None:
        channel.fetch_message = AsyncMock(side_effect=fetch_raises)
    h.client.get_channel = lambda _id: channel
    h.client.fetch_channel = AsyncMock(return_value=channel)
    h.connector = MagicMock()
    h.connector._reaction_wake = reaction_wake
    h.connector.emit = AsyncMock()
    return h


async def test_a_reaction_on_our_message_wakes_us():
    h = _handler()
    await h._wake_on_reaction(_payload(), "🔴")
    h.connector.emit.assert_awaited_once()
    msg = h.connector.emit.await_args.args[0]
    assert "🔴" in msg.content
    assert msg.reply_to == "7"
    assert msg.source == "user"
    assert msg.bot_account == "atlas"


async def test_a_reaction_on_another_bots_message_wakes_nobody():
    """The multi-bot guard. Every gateway client in this process runs the
    handler, so without it one reaction wakes the whole fleet."""
    h = _handler(author_id=OTHER_BOT)
    await h._wake_on_reaction(_payload(), "🔴")
    h.connector.emit.assert_not_awaited()


async def test_a_reaction_from_another_agent_is_ignored():
    """Agents react to each other's posts. Waking on that is an echo loop
    that costs a turn every time round."""
    h = _handler()
    await h._wake_on_reaction(_payload(member_bot=True), "🔴")
    h.connector.emit.assert_not_awaited()


async def test_a_dm_reaction_still_checks_the_reactor_is_human():
    """DM payloads carry no member object, so the bot check has to fall back
    to the user object rather than being skipped."""
    h = _handler()
    h.client.get_user = lambda _id: SimpleNamespace(bot=True,
                                                    display_name="data-bot")
    await h._wake_on_reaction(_payload(member=False), "🔴")
    h.connector.emit.assert_not_awaited()


async def test_a_dm_reaction_from_a_human_wakes_us():
    h = _handler()
    await h._wake_on_reaction(_payload(member=False), "🔴")
    h.connector.emit.assert_awaited_once()


async def test_an_unreadable_channel_is_quiet_not_an_error():
    """Every other gateway client also runs this handler against channels it
    cannot see. That is the normal case and must not log as a fault."""
    h = _handler(fetch_raises=PermissionError("Missing Access"))
    await h._wake_on_reaction(_payload(), "🔴")
    h.connector.emit.assert_not_awaited()


async def test_the_toggle_turns_it_off():
    h = _handler(reaction_wake=False)
    await h._wake_on_reaction(_payload(), "🔴")
    h.connector.emit.assert_not_awaited()


async def test_the_message_text_is_quoted_so_the_agent_knows_which_post():
    h = _handler()
    await h._wake_on_reaction(_payload(), "✅")
    content = h.connector.emit.await_args.args[0].content
    assert "clip 12" in content
    assert "7" in content


@pytest.mark.parametrize("emoji", ["🔴", "✅", "🎉", "😀"])
async def test_any_emoji_wakes_not_just_a_known_list(emoji):
    """'Always acknowledge' means the vocabulary is the reactor's choice, not
    a list this file has to keep up with."""
    h = _handler()
    await h._wake_on_reaction(_payload(), emoji)
    h.connector.emit.assert_awaited_once()


async def test_a_payload_without_a_member_attribute_does_not_crash():
    """Some payloads arrive without .member at all. Reaching for it directly
    raised inside on_raw_reaction_add, which would have taken the whole
    reaction path down rather than just this wake-up."""
    h = _handler()
    p = SimpleNamespace(user_id=HUMAN, channel_id=5, message_id=7, emoji="🔴")
    await h._wake_on_reaction(p, "🔴")
    h.connector.emit.assert_awaited_once()
