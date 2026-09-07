"""The goals feature's own posts: one author, and no agent turn.

Two defects from the first real goal run, both visible in one nomination:
the confirmation went out under whichever bot won the race to decide it, and
it then woke every participant as ordinary bot-to-bot mail and spent seven
turns on a card nobody could act on.
"""

from types import SimpleNamespace

import pytest

from src.core import goal_notice
from src.core import goals as store


@pytest.fixture(autouse=True)
def _isolated_goals_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "goals.db"))
    monkeypatch.setattr(store, "_db", None)
    store._cache.clear()
    yield
    if store._db is not None:
        store._db.close()
    store._db = None
    store._cache.clear()


# --- the marker ---

def test_marking_is_idempotent():
    once = goal_notice.mark("added to the goal")
    assert goal_notice.mark(once) == once


def test_marked_text_reads_the_same():
    assert goal_notice.mark("hello").endswith("hello")


def test_only_the_marker_counts():
    """Never guess from the shape of the text: a confirmation an agent typed
    by hand is still an agent talking, and must still get its turn."""
    assert goal_notice.is_system_notice(goal_notice.mark("✅ **kai** added"))
    assert not goal_notice.is_system_notice("✅ **kai** added to `g-x` (by @you).")
    assert not goal_notice.is_system_notice("")
    assert not goal_notice.is_system_notice(None)


def test_marker_survives_at_the_front_of_a_chunked_notice():
    """Chunking splits from the front, so a leading marker is on the chunk the
    inbound gate sees; a trailing one would ride the last chunk only."""
    marked = goal_notice.mark("x" * 4000)
    assert goal_notice.is_system_notice(marked[:1900])


async def test_goal_posts_are_marked(monkeypatch):
    """Every goal system notice goes out through one function, so marking it
    there is what makes the guarantee hold for cards nobody has written yet."""
    from src.tools import goals as goal_tools

    sent = {}

    async def fake_post(vault, endpoint, payload, bot=""):
        sent["content"] = payload["content"]
        return {"id": "42"}

    monkeypatch.setattr("src.tools.discord_tools._discord_post", fake_post)
    ctx = SimpleNamespace(vault=object())
    assert await goal_tools._post_to_channel(ctx, "chan", "the card") == "42"
    assert goal_notice.is_system_notice(sent["content"])


# --- one client acts on a reaction every client sees ---

def _bot(account_name="maya-bot", bot_user_id=999, agent_configs=None):
    from src.connectors.discord import DiscordBot
    b = DiscordBot.__new__(DiscordBot)
    b.account_name = account_name
    b.client = SimpleNamespace(user=SimpleNamespace(id=bot_user_id))
    b.connector = SimpleNamespace(
        _agent_configs=agent_configs or {
            "maya": {"routing": {"discord": {"account": "maya-bot"}}},
            "kai": {"routing": {"discord": {"account": "kai-bot"}}},
        })
    from src.connectors.discord import DiscordConnector
    b.connector._find_bot_for_agent = (
        lambda agent_id: DiscordConnector._find_bot_for_agent(b.connector, agent_id))
    return b


def _payload(message_author_id):
    return SimpleNamespace(message_id=7, channel_id=42, user_id=1,
                           message_author_id=message_author_id)


def test_only_the_cards_author_acts():
    bot = _bot(bot_user_id=999)
    assert bot._owns_goal_reaction(_payload(999), "maya") is True
    assert bot._owns_goal_reaction(_payload(1234), "maya") is False


def test_election_when_discord_omits_the_author():
    """message_author_id is Optional in the gateway payload. Without a
    fallback every client would skip and the confirmation would never post."""
    maya_bot = _bot(account_name="maya-bot", bot_user_id=999)
    kai_bot = _bot(account_name="kai-bot", bot_user_id=1000)
    assert maya_bot._owns_goal_reaction(_payload(None), "maya") is True
    assert kai_bot._owns_goal_reaction(_payload(None), "maya") is False


def test_election_with_no_owner_account_elects_nobody():
    bot = _bot()
    assert bot._owns_goal_reaction(_payload(None), "ghost") is False


async def test_a_losing_client_posts_nothing_and_decides_nothing():
    """The race is the defect: two clients both deciding meant the second one
    saw an already-decided nomination, and the first announced it under a name
    that had nothing to do with the goal."""
    goal = store.create_goal("Ship it", "d", "maya", "42", "u")
    store.add_nomination(goal["id"], "kai", "needed for the numbers")
    store.set_nomination_message(goal["id"], "kai", "7")

    posts = []
    bot = _bot(account_name="kai-bot", bot_user_id=1000)   # not the card's author
    bot._is_admin = lambda _uid: True
    bot.client.get_channel = lambda cid: SimpleNamespace(
        send=_recorder(posts))

    handled = await bot._handle_goal_reaction(_payload(999), "✅")
    assert handled is True          # still a goal card, not HITL's business
    assert posts == []
    assert store.get_nomination(goal["id"], "kai")["status"] == "pending"


async def test_the_author_decides_and_its_confirmation_is_marked():
    goal = store.create_goal("Ship it", "d", "maya", "42", "u")
    store.add_nomination(goal["id"], "kai", "needed for the numbers")
    store.set_nomination_message(goal["id"], "kai", "7")

    posts = []
    bot = _bot(account_name="maya-bot", bot_user_id=999)
    bot._is_admin = lambda _uid: True
    bot.client.get_channel = lambda cid: SimpleNamespace(send=_recorder(posts))

    assert await bot._handle_goal_reaction(_payload(999), "✅") is True
    assert store.get_nomination(goal["id"], "kai")["status"] == "approved"
    assert len(posts) == 1
    assert goal_notice.is_system_notice(posts[0])
    assert "kai" in posts[0] and "added to" in posts[0]


# --- a marked notice costs no turn ---

def _watching_bot():
    """A bot watching a channel, which is what a goal participant is."""
    from src.connectors.discord import DiscordBot
    b = DiscordBot.__new__(DiscordBot)
    b.account_name = "main"
    b.client = SimpleNamespace(
        user=SimpleNamespace(id=999, bot=True, name="Atlas", display_name="Atlas"))
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
        _agent_configs={"atlas": {"routing": {"discord": {
            "mentions": True, "watch_channels": ["555"]}}}},
        emit=emit,
    )
    return b, emitted


_ids = iter(range(1, 10_000))


def _bot_msg(content):
    return SimpleNamespace(
        id=next(_ids),
        author=SimpleNamespace(id=42, bot=True, display_name="Sender"),
        channel=SimpleNamespace(id=555, category_id=None, name="goal-ship-it"),
        guild=None, content=content, mentions=[], role_mentions=[],
        channel_mentions=[], attachments=[], reference=None,
    )


async def test_a_marked_notice_wakes_nobody():
    bot, emitted = _watching_bot()
    await bot.on_message(_bot_msg(goal_notice.mark("✅ **kai** added to `g-x`.")))
    assert emitted == []


async def test_an_ordinary_bot_message_in_the_same_room_still_lands():
    """The gate must cost the room nothing else: peer agents still talk here."""
    bot, emitted = _watching_bot()
    await bot.on_message(_bot_msg("I will take #38, starting now."))
    assert len(emitted) == 1


def _recorder(sink):
    async def send(content, **kwargs):
        sink.append(content)
        return SimpleNamespace(id=1)
    return send


# --- a mention of an agent the goal has not staffed ---

def _outsider_bot():
    """A bot whose agent is NOT routed for this goal channel."""
    b, emitted = _watching_bot()
    sent = []

    async def _send(content):
        sent.append(content)

    b.connector.get_agent_for_channel = lambda ch, acct, cat=None: None
    b.connector.is_goal_channel = lambda ch: str(ch) == "555"
    b.account_name = "atlas"
    return b, emitted, sent, _send


async def test_a_mention_of_an_unstaffed_agent_is_answered_not_dropped():
    """Silence reads as a broken bot and gets retried. Say it once instead."""
    bot, emitted, sent, send = _outsider_bot()
    msg = _bot_msg("@Atlas can you look at this?")
    msg.author = SimpleNamespace(id=42, bot=False, display_name="Sender")
    msg.channel = SimpleNamespace(id=555, category_id=None, name="goal-ship-it",
                                  send=send)
    msg.mentions = [bot.client.user]

    await bot.on_message(msg)

    assert emitted == []                       # no model turn for the outsider
    assert len(sent) == 1
    assert goal_notice.is_system_notice(sent[0])   # and none for the room
    assert "goal_add_member" in sent[0]


async def test_a_mention_outside_a_goal_room_is_untouched():
    """The rule is about goal rooms; ordinary channels keep their routing."""
    bot, emitted, sent, send = _outsider_bot()
    bot.connector.is_goal_channel = lambda ch: False
    msg = _bot_msg("@Atlas hello")
    msg.author = SimpleNamespace(id=42, bot=False, display_name="Sender")
    msg.channel = SimpleNamespace(id=777, category_id=None, name="general",
                                  send=send)
    msg.mentions = [bot.client.user]

    await bot.on_message(msg)
    assert sent == []                          # nothing said, nothing routed
