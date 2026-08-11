"""Bot presence: shows a 'working' status while busy, reverts when idle."""

from unittest.mock import AsyncMock, MagicMock

import discord

from src.connectors.discord import DiscordBot, _short_task_hint


def test_short_task_hint():
    assert _short_task_hint("can you scan for shelly devices?") == "can you scan for shelly devices?"
    long = "please do a very long thing " * 5
    h = _short_task_hint(long)
    assert len(h) <= 41 and h.endswith("…")
    # mentions stripped, whitespace collapsed
    assert _short_task_hint("<@123456789>   hey    there") == "hey there"


class _StubConnector:
    def __init__(self, presence=True):
        self.config = {"presence": presence}


def _bot(presence=True):
    bot = DiscordBot("main", _StubConnector(presence), admin_users=[])
    bot.client = MagicMock()
    bot.client.is_ready.return_value = True
    bot.client.change_presence = AsyncMock()
    return bot


async def test_working_then_idle():
    bot = _bot()
    await bot.task_started("thinking")
    assert bot.client.change_presence.await_count == 1
    status = bot.client.change_presence.await_args.kwargs["status"]
    assert status == discord.Status.dnd

    await bot.task_finished()
    assert bot.client.change_presence.await_count == 2
    last = bot.client.change_presence.await_args.kwargs
    assert last["status"] == discord.Status.online
    assert last["activity"] is None


async def test_refcount_no_flicker_on_concurrent_tasks():
    bot = _bot()
    await bot.task_started()          # 0 → 1: one presence change
    await bot.task_started()          # 1 → 2: no change (already busy)
    await bot.task_finished()         # 2 → 1: still busy, no change
    assert bot.client.change_presence.await_count == 1
    await bot.task_finished()         # 1 → 0: revert to idle
    assert bot.client.change_presence.await_count == 2


async def test_disabled_makes_no_calls():
    bot = _bot(presence=False)
    await bot.task_started("x")
    await bot.task_finished()
    assert bot.client.change_presence.await_count == 0


async def test_not_ready_is_safe():
    bot = _bot()
    bot.client.is_ready.return_value = False
    await bot.task_started()  # should not raise or call
    assert bot.client.change_presence.await_count == 0
    await bot.task_finished()  # cancels the heartbeat cleanly


async def test_shows_task_hint_in_status():
    bot = _bot()
    await bot.task_started("scan the network")
    activity = bot.client.change_presence.await_args.kwargs["activity"]
    assert "scan the network" in activity.name
    assert activity.name.startswith("🛠")
    await bot.task_finished()
