"""A human talking in a goal room is answered by the goal's owner, unmentioned.

Task #58 on g-harden-the-goals-feature. Before this, every participant took a
turn on an unmentioned human message in a live goal room and was told by the
phase protocol to reply NO_REPLY (the whole team's turns spent to answer
nothing), and once the goal was retired nobody was routed at all, so "why was
this closed?" went unanswered. Now exactly the owner hears it, in any status,
and the room stays writable for the owner's bot after it is archived.
"""

from types import SimpleNamespace

import pytest

from src.core import goals as store
from src.core.base import ToolContext


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


def _goal(status="executing", channel="555", owner="maya", member="kai"):
    goal = store.create_goal("Ship it", "", owner, channel, "user1")
    for step in {"proposed": [], "executing": ["brainstorm", "strategy", "executing"],
                 "done": ["brainstorm", "strategy", "executing", "done"],
                 "abandoned": ["abandoned"]}[status]:
        goal = store.update_goal(goal["id"], owner, status=step)
    if member:
        store.add_participant(goal["id"], member)
    store._cache.clear()
    return goal


# --- store -------------------------------------------------------------------

def test_audience_covers_every_status_and_no_ordinary_channel():
    goal = _goal("executing")
    aud = store.goal_audience_for_channel("555")
    assert aud["owner"] == "maya" and set(aud["participants"]) == {"maya", "kai"}
    assert aud["status"] == "executing"
    store.update_goal(goal["id"], "maya", status="done")
    aud = store.goal_audience_for_channel("555")
    assert aud["status"] == "done" and aud["owner"] == "maya"
    assert store.routed_participants_for_channel("555") == []   # watching ends
    assert store.goal_audience_for_channel("999") is None


def test_audience_follows_an_owner_handover():
    goal = _goal("executing")
    store.reassign_owner(goal["id"], "user1", "kai")
    assert store.goal_audience_for_channel("555")["owner"] == "kai"


def test_context_tells_the_owner_it_is_addressed_and_members_they_are_not():
    _goal("executing")
    assert "reach only you" in store.build_goal_context("maya", "555")
    assert "answered by the owner" in store.build_goal_context("kai", "555")


def test_context_for_a_closed_goal_says_answer_do_not_reopen():
    _goal("done")
    ctx = store.build_goal_context("maya", "555")
    assert 'status="done"' in ctx and "CLOSED (done)" in ctx
    assert "do not reopen" in ctx and "reach only you" in ctx
    _goal("abandoned", channel="556")
    assert "CLOSED (abandoned)" in store.build_goal_context("maya", "556")


def test_context_for_a_proposed_goal_says_a_question_is_not_approval():
    _goal("proposed")
    ctx = store.build_goal_context("maya", "555")
    assert "not" in ctx and "approval" in ctx and "✅" in ctx


# --- connector routing -------------------------------------------------------

def _conn():
    from src.connectors.discord import DiscordConnector
    conn = DiscordConnector.__new__(DiscordConnector)
    conn._agent_configs = {
        "maya": {"routing": {"discord": {"account": "maya-bot", "mentions": True}}},
        "kai": {"routing": {"discord": {"account": "kai-bot", "mentions": True}}},
        "rio": {"routing": {"discord": {"account": "rio-bot", "mentions": True}}},
    }
    return conn


def test_a_retired_room_still_routes_only_its_participants():
    """It used to fall to the wildcard, so a mention there woke whichever
    agent the bot happened to default to, and an unmentioned question woke
    nobody."""
    _goal("done")
    conn = _conn()
    assert conn.get_agent_for_channel("555", "maya-bot") == "maya"
    assert conn.get_agent_for_channel("555", "kai-bot") == "kai"
    assert conn.get_agent_for_channel("555", "rio-bot") is None
    assert conn.is_goal_channel("555")
    assert not conn.is_goal_channel("999")


# --- the bot: who takes the turn ---------------------------------------------

_ids = iter(range(1, 10_000))


def _bot(account, agent_configs):
    from src.connectors.discord import DiscordBot
    b = DiscordBot.__new__(DiscordBot)
    b.account_name = account
    b.client = SimpleNamespace(
        user=SimpleNamespace(id=hash(account) % 10_000, bot=True, name=account))
    b._seen_message_ids = set()
    b._seen_message_cap = 1000
    b._bot_chain = {}
    b._bot_loop_hits = {}
    b._bot_cooldown = {}
    b._bot_recent_content = {}
    emitted = []

    async def emit(msg):
        emitted.append(msg)

    from src.connectors.discord import DiscordConnector
    conn = DiscordConnector.__new__(DiscordConnector)
    conn._agent_configs = agent_configs
    conn.config = {}
    conn.emit = emit
    b.connector = conn
    return b, emitted


_CFG = {
    "maya": {"routing": {"discord": {"account": "maya-bot", "mentions": True}}},
    "kai": {"routing": {"discord": {"account": "kai-bot", "mentions": True}}},
    "rio": {"routing": {"discord": {"account": "rio-bot", "mentions": True}}},
}


def _human(content="why is this taking so long?", channel=555, mentions=(),
           sent=None):
    ch = SimpleNamespace(id=channel, category_id=None, name="goal-ship-it")
    if sent is not None:
        async def _send(text):
            sent.append(text)
        ch.send = _send
    return SimpleNamespace(
        id=next(_ids),
        author=SimpleNamespace(id=42, bot=False, display_name="Kristian"),
        channel=ch, guild=None, content=content, mentions=list(mentions),
        role_mentions=[], channel_mentions=[], attachments=[], reference=None)


def _bot_post(content="I will take #3.", channel=555):
    return SimpleNamespace(
        id=next(_ids),
        author=SimpleNamespace(id=77, bot=True, display_name="Kai"),
        channel=SimpleNamespace(id=channel, category_id=None, name="goal-ship-it"),
        guild=None, content=content, mentions=[], role_mentions=[],
        channel_mentions=[], attachments=[], reference=None)


async def _fleet_hears(msg):
    """Run the same message through every bot; return {agent: IncomingMessage}."""
    heard = {}
    for account in ("maya-bot", "kai-bot", "rio-bot"):
        bot, emitted = _bot(account, _CFG)
        await bot.on_message(msg)
        assert len(emitted) <= 1
        if emitted:
            heard[account] = emitted[0]
    return heard


async def test_unmentioned_human_in_a_live_room_reaches_only_the_owner():
    _goal("executing")
    heard = await _fleet_hears(_human())
    assert list(heard) == ["maya-bot"]
    assert heard["maya-bot"].watched is False       # addressed, not overheard
    assert heard["maya-bot"].source == "user"


async def test_unmentioned_human_in_a_closed_room_reaches_the_owner():
    """The immediate case: a follow-up question in a completed goal's room."""
    _goal("done")
    heard = await _fleet_hears(_human("why was this closed?"))
    assert list(heard) == ["maya-bot"]
    assert heard["maya-bot"].watched is False


async def test_unmentioned_human_in_a_proposed_room_reaches_the_owner():
    _goal("proposed")
    heard = await _fleet_hears(_human("who is kai and why is he on this?"))
    assert list(heard) == ["maya-bot"]


async def test_the_owner_seat_decides_not_who_created_the_goal():
    goal = _goal("executing")
    store.reassign_owner(goal["id"], "user1", "kai")
    store._cache.clear()
    heard = await _fleet_hears(_human())
    assert list(heard) == ["kai-bot"]


async def test_a_mentioned_member_still_answers():
    _goal("executing")
    kai_bot, emitted = _bot("kai-bot", _CFG)
    msg = _human("@Kai status?", mentions=[kai_bot.client.user])
    await kai_bot.on_message(msg)
    assert len(emitted) == 1 and emitted[0].watched is False


async def test_bot_posts_keep_their_audience_in_a_live_room():
    """Collaboration between participants is unchanged: every member still
    overhears a peer's post in a live room, and nobody outside does."""
    _goal("executing")
    heard = await _fleet_hears(_bot_post())
    assert set(heard) == {"maya-bot", "kai-bot"}
    assert all(m.watched for m in heard.values())


async def test_bot_posts_wake_nobody_in_a_closed_room():
    _goal("done")
    assert await _fleet_hears(_bot_post()) == {}


async def test_an_ordinary_channel_is_untouched():
    _goal("executing")
    # No goal owns 777: mentions-only agents ignore an unmentioned human there.
    assert await _fleet_hears(_human(channel=777)) == {}


async def test_an_outsider_mentioned_in_a_closed_room_is_told_who_answers():
    _goal("done")
    rio_bot, emitted = _bot("rio-bot", _CFG)
    sent: list[str] = []
    msg = _human("@Rio can you explain?", mentions=[rio_bot.client.user], sent=sent)
    await rio_bot.on_message(msg)
    assert emitted == []
    assert len(sent) == 1 and "closed" in sent[0] and "maya" in sent[0]
    assert "goal_add_member" not in sent[0]


# --- archive keeps the owner's bot able to post ------------------------------

def _ctx():
    return ToolContext(agent_id="maya", channel_id="home", user_id="u", vault=object())


async def test_archive_lets_the_owner_bot_post_and_replaces_its_old_overwrite(monkeypatch):
    from src.tools import goals as tools
    patched: list[tuple[str, dict]] = []

    async def _get(vault, endpoint, bot=""):
        if endpoint == "/users/@me":
            return {"id": "bot-maya"}
        return {"guild_id": "g1", "topic": "Goal workstream: X",
                "permission_overwrites": [
                    {"id": "bot-maya", "type": 1, "allow": "1024", "deny": "0"},
                    {"id": "bot-9", "type": 1, "allow": "2048", "deny": "0"}]}

    async def _patch(vault, endpoint, payload, bot=""):
        patched.append((endpoint, payload))
        return {"id": "555"}

    monkeypatch.setattr("src.tools.discord_tools._discord_get", _get)
    monkeypatch.setattr("src.tools.discord_tools._discord_patch", _patch)
    monkeypatch.setattr("src.lib.discord_auth.resolve_bot_token",
                        lambda vault, bot="", agent_id="": SimpleNamespace(
                            token="t", account="maya-bot", error=None))
    goal = _goal("done")
    note = await tools._archive_channel(_ctx(), goal)
    assert note.startswith("channel archived read-only") and "maya may still post" in note
    by_id = {o["id"]: o for o in patched[0][1]["permission_overwrites"]}
    assert int(by_id["g1"]["deny"]) & tools._SEND_MESSAGES
    assert by_id["bot-maya"] == {"id": "bot-maya", "type": 1,
                                 "allow": str(tools._SEND_MESSAGES), "deny": "0"}
    assert by_id["bot-9"]["allow"] == "2048"                    # untouched
    assert sum(1 for o in patched[0][1]["permission_overwrites"]
               if o["id"] == "bot-maya") == 1


async def test_archive_still_closes_the_room_when_the_owner_bot_is_unknown(monkeypatch):
    from src.tools import goals as tools
    patched: list[dict] = []

    async def _get(vault, endpoint, bot=""):
        return {"guild_id": "g1", "topic": "", "permission_overwrites": []}

    async def _patch(vault, endpoint, payload, bot=""):
        patched.append(payload)
        return {"id": "555"}

    monkeypatch.setattr("src.tools.discord_tools._discord_get", _get)
    monkeypatch.setattr("src.tools.discord_tools._discord_patch", _patch)
    monkeypatch.setattr("src.lib.discord_auth.resolve_bot_token",
                        lambda vault, bot="", agent_id="": SimpleNamespace(
                            token=None, account="", error="no token"))
    note = await tools._archive_channel(_ctx(), _goal("done"))
    assert note.startswith("channel archived read-only") and "not resolved" in note
    assert [o["id"] for o in patched[0]["permission_overwrites"]] == ["g1"]


def test_closing_notice_names_the_owner():
    """Done asks the user for a verdict and says who follows up on ❌;
    abandoned invites questions and says who answers them."""
    from src.tools import goals as tools
    text = tools._closing_text(_goal("done"))
    assert tools.VERDICT_ASK in text and "**maya** asks what is missing" in text
    text = tools._closing_text(_goal("abandoned"))
    assert "questions are welcome" in text and "**maya**" in text
    assert "no mention needed" in text
