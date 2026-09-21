"""The user's verdict on a goal's closing summary.

`goal_set status=done` is the owner's claim that a goal is reached. It is not
the user's. The summary the owner posts last carries ✅/❌, and this is what
those mean: ✅ removes the room, ❌ hands it back to the owner to ask what is
missing. Both are irreversible in one direction or the other, so the tests
here care most about the things that must NOT happen — no delete without an
admin saying so, no delete of a borrowed channel, no second verdict, and no
room removed while the goal still looks unasked in the store.
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


def _recorder(sink):
    async def send(content, **kwargs):
        sink.append(content)
        return SimpleNamespace(id=1)
    return send


class _Manager:
    def __init__(self):
        self.delivered = []

    async def deliver_inter_agent_message(self, agent, sender, text, priority=0):
        self.delivered.append((agent, text))


def _bot(posts, elsewhere=None, alert="990", account="maya-bot", bot_user_id=999):
    """A DiscordBot with just enough around it to run the reaction handler."""
    from src.connectors.discord import DiscordBot, DiscordConnector
    b = DiscordBot.__new__(DiscordBot)
    b.account_name = account
    b.client = SimpleNamespace(user=SimpleNamespace(id=bot_user_id))
    b.connector = SimpleNamespace(
        vault=object(),
        _agent_manager=_Manager(),
        _full_config={"security": {"alert_channel": alert}},
        _agent_configs={"maya": {"routing": {"discord": {"account": "maya-bot"}}},
                        "kai": {"routing": {"discord": {"account": "kai-bot"}}}})
    b.connector._find_bot_for_agent = (
        lambda agent_id: DiscordConnector._find_bot_for_agent(b.connector, agent_id))
    room = SimpleNamespace(send=_recorder(posts))
    other = SimpleNamespace(send=_recorder(elsewhere if elsewhere is not None else []))
    b.client.get_channel = lambda cid: other if str(cid) == alert else room
    b._is_admin = lambda _uid: True
    return b


def _payload(message_author_id=999, user_id=1):
    return SimpleNamespace(message_id=7, channel_id=42, user_id=user_id,
                           message_author_id=message_author_id)


def _done_goal(summary="🏁 DONE: Ship it\nDelivered: everything", anchored=False):
    """A goal retired to 'done' with its summary posted as message id 7."""
    goal = store.create_goal("Ship it", "d", "maya", "42", "u", anchored=anchored)
    for status in ("brainstorm", "strategy", "executing", "done"):
        goal = store.update_goal(goal["id"], "maya", status=status)
    return store.update_goal(goal["id"], "maya", closing_message_id="7",
                             summary=summary)


def _deleter(sink, ok=True):
    async def _delete(vault, endpoint, bot=""):
        sink.append((endpoint, bot))
        return {"success": True} if ok else {"error": True, "detail": "Missing Access"}
    return _delete


# --- ✅: reached ------------------------------------------------------------

async def test_reached_records_places_drops_and_deletes(monkeypatch):
    """The whole ✅ path in the order that survives a failure: the verdict is
    a column first, the summary is somewhere else second, the tasks close
    third, and the room goes last."""
    deleted = []
    monkeypatch.setattr("src.tools.discord_tools._discord_delete", _deleter(deleted))
    goal = _done_goal()
    store.add_task(goal["id"], "left over", "", "kai", "maya")
    store.add_task(goal["id"], "also done", "", "kai", "maya")
    store.update_task(2, "maya", status="done")
    posts, elsewhere = [], []

    assert await _bot(posts, elsewhere)._handle_goal_reaction(_payload(), "✅") is True

    g = store.get_goal(goal["id"])
    assert g["verdict"] == "reached" and g["verdict_by"] == "1" and g["verdict_at"] > 0
    assert g["status"] == "done"
    assert deleted == [("/channels/42", "maya-bot")]
    # the record of where the work happened outlives the room
    assert g["channel_id"] == "42"
    # the summary is placed where it can be read after the room is gone
    assert len(elsewhere) == 1 and goal_notice.is_system_notice(elsewhere[0])
    assert "Delivered: everything" in elsewhere[0] and "confirmed reached" in elsewhere[0]
    # leftovers are closed with one reason, finished work is untouched
    assert store.list_tasks(goal["id"]) == []
    assert store.get_task(1)["drop_reason"] == "goal closed"
    assert store.get_task(2)["status"] == "done"
    kinds = [r[0] for r in store._get_db().execute(
        "SELECT kind FROM goal_events WHERE goal_id=? AND kind='channel_deleted'",
        (goal["id"],))]
    assert kinds == ["channel_deleted"]


async def test_a_borrowed_room_is_never_deleted(monkeypatch):
    """An anchored goal sits in its proposer's home channel. The goal closes;
    somebody else's room is not the goal's to remove."""
    deleted = []
    monkeypatch.setattr("src.tools.discord_tools._discord_delete", _deleter(deleted))
    goal = _done_goal(anchored=True)
    posts = []

    assert await _bot(posts)._handle_goal_reaction(_payload(), "✅") is True

    assert deleted == []
    assert store.get_goal(goal["id"])["verdict"] == "reached"
    assert len(posts) == 1 and "borrowed" in posts[0]


async def test_a_failed_delete_still_leaves_the_verdict_and_says_so(monkeypatch):
    deleted = []
    monkeypatch.setattr("src.tools.discord_tools._discord_delete",
                        _deleter(deleted, ok=False))
    goal = _done_goal()
    posts = []

    assert await _bot(posts)._handle_goal_reaction(_payload(), "✅") is True

    assert store.get_goal(goal["id"])["verdict"] == "reached"
    assert len(posts) == 1 and "could not be deleted" in posts[0]
    assert goal_notice.is_system_notice(posts[0])


async def test_a_missing_alert_channel_does_not_block_the_removal(monkeypatch):
    """The goal record keeps the summary, so nowhere to re-post it is a log
    line, not a reason to ignore what the user asked for."""
    deleted = []
    monkeypatch.setattr("src.tools.discord_tools._discord_delete", _deleter(deleted))
    goal = _done_goal()
    posts = []
    bot = _bot(posts, alert="")
    bot.client.get_channel = lambda cid: SimpleNamespace(send=_recorder(posts))

    assert await bot._handle_goal_reaction(_payload(), "✅") is True
    assert deleted == [("/channels/42", "maya-bot")]
    assert store.get_goal(goal["id"])["summary"].startswith("🏁 DONE")


# --- ❌: not reached --------------------------------------------------------

async def test_not_reached_reopens_and_wakes_the_owner(monkeypatch):
    deleted = []
    monkeypatch.setattr("src.tools.discord_tools._discord_delete", _deleter(deleted))
    goal = _done_goal()
    store.add_task(goal["id"], "left over", "", "kai", "maya")
    posts = []
    bot = _bot(posts)

    assert await bot._handle_goal_reaction(_payload(), "❌") is True

    g = store.get_goal(goal["id"])
    assert g["status"] == "executing"
    assert deleted == []
    # reopening undoes the close: the next retirement must post its own summary
    assert g["closing_message_id"] == "" and g["verdict"] == ""
    assert store.goal_by_closing_message("7") is None
    # nothing was dropped, so nothing has to be un-dropped
    assert [t["id"] for t in store.list_tasks(goal["id"])] == [1]
    assert len(posts) == 1 and "not reached" in posts[0]
    agent, text = bot.connector._agent_manager.delivered[0]
    assert agent == "maya" and "what is missing" in text
    # the verdict survives as an event even though the column was cleared
    kinds = [r[0] for r in store._get_db().execute(
        "SELECT payload FROM goal_events WHERE goal_id=? AND kind='verdict'",
        (goal["id"],))]
    assert kinds == ["not_reached"]


# --- who may answer, and how often -----------------------------------------

async def test_only_an_admin_may_answer(monkeypatch):
    """Same bar as HITL. A verdict deletes a channel; it is not any reader's
    call. The handler still claims the message so it never falls through to
    HITL and gets treated as an approval of something else."""
    deleted = []
    monkeypatch.setattr("src.tools.discord_tools._discord_delete", _deleter(deleted))
    goal = _done_goal()
    posts = []
    bot = _bot(posts)
    bot._is_admin = lambda _uid: False

    assert await bot._handle_goal_reaction(_payload(user_id=4242), "✅") is True
    assert deleted == [] and posts == []
    assert store.get_goal(goal["id"])["verdict"] == ""


async def test_only_the_summarys_author_acts(monkeypatch):
    """Every gateway client sees the reaction; one must act. Two deleting the
    same channel is two 404s and a confirmation under the wrong name."""
    deleted = []
    monkeypatch.setattr("src.tools.discord_tools._discord_delete", _deleter(deleted))
    goal = _done_goal()
    posts = []
    bot = _bot(posts, account="kai-bot", bot_user_id=1000)

    assert await bot._handle_goal_reaction(_payload(message_author_id=999), "✅") is True
    assert deleted == [] and posts == []
    assert store.get_goal(goal["id"])["verdict"] == ""


async def test_a_second_reaction_changes_nothing(monkeypatch):
    deleted = []
    monkeypatch.setattr("src.tools.discord_tools._discord_delete", _deleter(deleted))
    goal = _done_goal()
    posts, elsewhere = [], []
    bot = _bot(posts, elsewhere)

    await bot._handle_goal_reaction(_payload(), "✅")
    await bot._handle_goal_reaction(_payload(), "✅")
    await bot._handle_goal_reaction(_payload(), "❌")

    assert len(deleted) == 1 and len(elsewhere) == 1
    assert store.get_goal(goal["id"])["status"] == "done"
    assert store.get_goal(goal["id"])["verdict"] == "reached"


async def test_a_reaction_on_an_ordinary_message_is_not_ours():
    """Unclaimed, so HITL still sees it. An empty closing_message_id must not
    match every goal that never posted one."""
    _done_goal()
    store.create_goal("No summary yet", "d", "maya", "43", "u")
    bot = _bot([])
    payload = SimpleNamespace(message_id=12345, channel_id=42, user_id=1,
                              message_author_id=999)
    assert await bot._handle_goal_reaction(payload, "✅") is False
    assert store.goal_by_closing_message("") is None
