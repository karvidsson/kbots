"""watch_channels routing — a watching agent hears every message in the
channel (human or bot, mentioned or not); everything else is unchanged."""

from types import SimpleNamespace

from src.connectors.discord import _BOT_CHAIN_LIMIT, DiscordBot

WATCHED = "555"
OTHER = "777"


def _bot(routing):
    b = DiscordBot.__new__(DiscordBot)
    b.account_name = "main"
    b.client = SimpleNamespace(
        user=SimpleNamespace(id=999, bot=True, name="Atlas", display_name="Atlas")
    )
    b._seen_message_ids = set()
    b._seen_message_cap = 1000
    b._bot_chain = {}
    b._bot_loop_hits = {}
    b._bot_cooldown = {}
    b._bot_recent_content = {}
    emitted = []

    async def emit(msg):
        emitted.append(msg)

    b.connector = SimpleNamespace(
        config={},
        get_agent_for_channel=lambda ch, acct, cat=None: "atlas",
        _agent_configs={"atlas": {"routing": {"discord": routing}}},
        emit=emit,
    )
    return b, emitted


_next_id = iter(range(1, 10_000))


def _msg(content="hello", channel_id=555, author_bot=False, mentions=(), author_id=42):
    return SimpleNamespace(
        id=next(_next_id),
        author=SimpleNamespace(id=author_id, bot=author_bot, display_name="Sender"),
        channel=SimpleNamespace(id=channel_id, category_id=None, name="migrate-agent"),
        guild=None,
        content=content,
        mentions=list(mentions),
        role_mentions=[],
        channel_mentions=[],
        attachments=[],
        reference=None,
    )


ROUTING = {"mentions": True, "watch_channels": [WATCHED]}


async def test_watched_channel_delivers_unmentioned_bot_message():
    bot, emitted = _bot(ROUTING)
    await bot.on_message(_msg("status update", author_bot=True))
    assert len(emitted) == 1
    assert emitted[0].watched is True


async def test_unwatched_channel_still_drops_unmentioned_bot_message():
    bot, emitted = _bot(ROUTING)
    await bot.on_message(_msg("status update", channel_id=777, author_bot=True))
    assert emitted == []


async def test_watched_channel_bypasses_mentions_only_for_humans():
    bot, emitted = _bot(ROUTING)
    await bot.on_message(_msg("moving the migration along"))
    assert len(emitted) == 1
    assert emitted[0].watched is True


async def test_unwatched_channel_still_requires_mention():
    bot, emitted = _bot(ROUTING)
    await bot.on_message(_msg("hello", channel_id=777))
    assert emitted == []


async def test_mentioned_message_in_watched_channel_is_not_flagged_watched():
    bot, emitted = _bot(ROUTING)
    me = bot.client.user
    await bot.on_message(_msg("ping", mentions=[me]))
    assert len(emitted) == 1
    assert emitted[0].watched is False


async def test_chain_guard_still_applies_in_watched_channel():
    bot, emitted = _bot(ROUTING)
    # Distinct senders and content so the per-pair loop guard stays out of
    # the way — this isolates the per-channel chain breaker.
    for i in range(_BOT_CHAIN_LIMIT + 3):
        await bot.on_message(
            _msg(f"progress report {i}", author_bot=True, author_id=1000 + i)
        )
    assert len(emitted) == _BOT_CHAIN_LIMIT
