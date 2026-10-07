"""Owner decisions through real SQLite, connector controls and loopback tool calls."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest

from src.connectors.discord_owner_asks import DiscordOwnerAsks, register_pending, send_report
from src.core import goals
from src.core.base import MessageDelivery, ToolContext
from src.core.hitl import HITLGate
from src.core.internal_api import InternalAPI
from src.core.owner_asks import AskStore, pending_report, settings
from src.tools.owner_asks import ask_owner

OWNER, OTHER, BOT = 1000000000000000001, 1000000000000000002, 1000000000000000003
CHANNEL, GUILD = 1000000000000000004, 1000000000000000005


class Message:
    def __init__(self, channel, number, content=None, embed=None, view=None, **kwargs):
        self.id, self.channel, self.guild = number, channel, channel.guild
        self.author = SimpleNamespace(id=BOT, bot=True)
        self.content, self.embeds, self.view = content or "", [embed] if embed else [], view
        self.webhook_id = None
        self.add_reaction = AsyncMock()
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.content = kwargs.get("content") or ""
        self.embeds = [kwargs["embed"]] if kwargs.get("embed") else []
        self.view = kwargs.get("view")


class Channel:
    def __init__(self):
        self.id = CHANNEL
        self.guild = SimpleNamespace(id=GUILD, me=SimpleNamespace(id=BOT))
        self.messages = []
        self.embed_links = True
        self.uncertain = False
        self.fail_seed = False

    def permissions_for(self, member):
        return SimpleNamespace(embed_links=self.embed_links)

    async def send(self, content=None, **kwargs):
        message = Message(self, 2000 + len(self.messages), content, **kwargs)
        if self.fail_seed:
            message.add_reaction.side_effect = RuntimeError("seed failed")
        self.messages.append(message)
        if self.uncertain:
            raise TimeoutError("response lost after send")
        return message

    async def fetch_message(self, message_id):
        return next(m for m in self.messages if m.id == message_id)

    async def history(self, limit):
        for message in reversed(self.messages[-limit:]):
            yield message


@pytest.fixture
async def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(goals, "DB_PATH", str(tmp_path / "goals.db"))
    monkeypatch.setattr(goals, "_db", None)
    goals._invalidate_cache()
    channel = Channel()
    owner = SimpleNamespace(id=OWNER, bot=False, send=AsyncMock())
    client = SimpleNamespace(
        user=SimpleNamespace(id=BOT),
        get_channel=Mock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
        fetch_user=AsyncMock(return_value=owner),
        add_view=Mock(),
    )
    bot = SimpleNamespace(account_name="example", client=client)
    connector = SimpleNamespace(bots={"example": bot}, _reserved_alert=Mock(return_value=False))
    bot.connector = connector
    manager = SimpleNamespace(
        agent_configs={"sample": {}, "second": {}},
        _resolve_home_channel=AsyncMock(return_value=("discord", str(CHANNEL), "example")),
        handle_message=AsyncMock(return_value=MessageDelivery(True)),
    )
    config = {"admin_users": {"discord": [str(OWNER)]}, "waiting_on_you": {"digest_hour": 23}}
    config["connectors"] = {"discord": {"guild_id": str(GUILD)}}
    db = await aiosqlite.connect(tmp_path / "engine.db")
    connector._hitl = HITLGate({"approvers": [str(OWNER)], "timeout": 1800}, db)
    await connector._hitl.init_schema()
    path = tmp_path / "asks.db"
    service = DiscordOwnerAsks(connector, manager, config, path)
    service.ready_accounts.add("example")
    connector._owner_asks = manager._owner_asks = service
    result = SimpleNamespace(
        service=service,
        store=service.store,
        channel=channel,
        owner=owner,
        bot=bot,
        connector=connector,
        manager=manager,
        config=config,
        path=path,
    )
    yield result
    service.store.close()
    await db.close()
    if goals._db is not None:
        goals._db.close()
    goals._db = None
    goals._invalidate_cache()


async def create(f, **kwargs):
    result = await f.service.ask(
        "sample",
        kwargs.pop("question", "Publish the reviewed draft?"),
        kwargs.pop("default", "Keep the draft unpublished."),
        **kwargs,
    )
    return f.store.get(result["id"])


def reaction(row, user=OWNER, bot=False, emoji="✅", **changes):
    values = dict(
        message_id=int(row["message_id"]),
        user_id=user,
        member=SimpleNamespace(id=user, bot=bot),
        channel_id=CHANNEL,
        guild_id=GUILD,
        emoji=emoji,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def interaction(f, row, user=OWNER, **changes):
    values = dict(
        user=SimpleNamespace(id=user, bot=False),
        client=f.bot.client,
        channel_id=CHANNEL,
        guild_id=GUILD,
        message=f.channel.messages[0],
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    values.update(changes)
    return SimpleNamespace(**values)


async def drain_events(f):
    await f.service.tick()
    if f.service.events:
        await asyncio.gather(*list(f.service.events.values()))


@pytest.mark.parametrize("emoji,answer", [("✅", "Yes"), ("🔴", "No")])
async def test_reaction_records_edits_and_delivers_exact_answer(fixture, emoji, answer):
    f = fixture
    row = await create(f)
    assert len(f.channel.messages) == 1
    assert f.channel.messages[0].add_reaction.call_count == 2
    assert "Publish" in pending_report(f.store.rows()) and str(row["message_id"]) in pending_report(f.store.rows())
    assert await f.service.react(f.bot, reaction(row, emoji=emoji))
    result = f.store.get(row["id"])
    assert result["state"] == "answered" and result["answer"] == answer
    assert not f.store.rows() and len(f.channel.messages) == 1
    assert f.channel.messages[0].embeds[0].title == "Answered"
    await drain_events(f)
    f.manager.handle_message.assert_awaited_once()
    agent, message = f.manager.handle_message.call_args.args
    assert agent == "sample" and message.user_id == str(OWNER) and message.bot_account == "example"
    assert json.loads(message.content.split("\n", 1)[1])["answer"] == answer
    assert message._owner_ask_id == row["id"]
    assert await f.service.react(f.bot, reaction(row))  # no generic approval wake on closed cards
    await drain_events(f)
    f.manager.handle_message.assert_awaited_once()


@pytest.mark.parametrize(
    "change",
    [
        "bot",
        "other",
        "foreign_message",
        "foreign_channel",
        "foreign_guild",
        "foreign_author",
        "webhook",
        "other_account",
        "emoji",
    ],
)
async def test_foreign_reactions_never_answer(fixture, change):
    f = fixture
    row = await create(f)
    payload = reaction(row)
    bot = f.bot
    if change == "bot":
        payload.member.bot = True
    if change == "other":
        payload.user_id = payload.member.id = OTHER
    if change == "foreign_message":
        payload.message_id += 1
    if change == "foreign_channel":
        payload.channel_id += 1
    if change == "foreign_guild":
        payload.guild_id += 1
    if change == "foreign_author":
        f.channel.messages[0].author.id = OTHER
    if change == "webhook":
        f.channel.messages[0].webhook_id = 123
    if change == "other_account":
        bot = SimpleNamespace(account_name="other", client=f.bot.client)
    if change == "emoji":
        payload.emoji = "👍"
    await f.service.react(bot, payload)
    assert f.store.get(row["id"])["state"] == "open"
    assert f.store.db.execute("SELECT count(*) FROM owner_ask_events").fetchone()[0] == 0


async def test_expired_reaction_records_stale_never_approval(fixture):
    f = fixture
    row = await create(f)
    f.store.db.execute("UPDATE owner_asks SET stale_at=?", (time.time() - 1,))
    await f.service.react(f.bot, reaction(row))
    result = f.store.get(row["id"])
    assert result["state"] == "stale" and result["answer"] is None and result["answered_by"] is None
    await drain_events(f)
    message = f.manager.handle_message.call_args.args[1]
    assert message.source == "schedule" and message.user_id != str(OWNER)
    assert "NO approval" in message.content
    assert f.channel.messages[0].embeds[0].title == "Expired without an answer"
    f.owner.send.assert_not_awaited()


async def test_buttons_persist_owner_bound_and_close_once(fixture):
    f = fixture
    row = await create(f, options=["Keep draft", "Publish"])
    assert f.channel.messages[0].add_reaction.call_count == 0
    assert all(not b.disabled for b in f.channel.messages[0].view.children)
    other = interaction(f, row, user=OTHER)
    await f.service.click(row["id"], row["revision"], 1, other)
    assert other.response.send_message.call_args.kwargs["ephemeral"] is True
    assert f.store.get(row["id"])["state"] == "open"
    await f.service.click(row["id"], 9, 1, interaction(f, row))
    assert f.store.get(row["id"])["state"] == "open"
    # Real database close/reopen, then reconstruct persistent view without a send.
    f.store.close()
    f.service.store = f.store = AskStore(f.path)
    await f.service.start(f.bot)
    f.service.task.cancel()
    await asyncio.gather(f.service.task, return_exceptions=True)
    f.service.task = None
    f.bot.client.add_view.assert_called_once()
    restored = f.bot.client.add_view.call_args.args[0]
    assert f.bot.client.add_view.call_args.kwargs["message_id"] == int(row["message_id"])
    await restored.children[1].callback(interaction(f, row))
    assert f.store.get(row["id"])["answer"] == "Publish"
    assert all(b.disabled for b in f.channel.messages[0].view.children)
    await drain_events(f)
    f.manager.handle_message.assert_awaited_once()
    assert len(f.channel.messages) == 1


@pytest.mark.parametrize("fault", ["client", "guild", "channel", "author", "bot"])
async def test_button_binding(fixture, fault):
    f = fixture
    row = await create(f, options=["A", "B"])
    i = interaction(f, row)
    if fault == "client":
        i.client = object()
    if fault == "guild":
        i.guild_id += 1
    if fault == "channel":
        i.channel_id += 1
    if fault == "author":
        i.message.author.id = OTHER
    if fault == "bot":
        i.user.bot = True
    await f.service.click(row["id"], 1, 0, i)
    assert f.store.get(row["id"])["state"] == "open"
    assert i.response.send_message.call_args.kwargs["ephemeral"]


async def test_reply_must_reference_card_and_be_from_recipient(fixture):
    f = fixture
    row = await create(f)
    message = SimpleNamespace(
        reference=None,
        author=SimpleNamespace(id=OWNER, bot=False),
        channel=f.channel,
        guild=f.channel.guild,
        content="Wait until Friday.",
        add_reaction=AsyncMock(),
    )
    assert not await f.service.reply(f.bot, message)
    message.reference = SimpleNamespace(message_id=row["message_id"])
    message.author.id = OTHER
    assert await f.service.reply(f.bot, message)
    assert f.store.get(row["id"])["state"] == "open"
    message.author.id = OWNER
    assert await f.service.reply(f.bot, message)
    assert f.store.get(row["id"])["answer"] == "Wait until Friday."
    await drain_events(f)
    f.manager.handle_message.assert_awaited_once()


async def test_seed_failure_and_plain_fallback_preserve_ask(fixture):
    f = fixture
    f.channel.fail_seed = True
    f.channel.embed_links = False
    row = await create(f)
    assert row["state"] == "open" and row["seed_failed"] == 1
    assert len(f.channel.messages) == 1 and not f.channel.messages[0].embeds
    assert "Reactions could not be added" in f.channel.messages[0].content
    assert "If no reply: Keep the draft unpublished." in f.channel.messages[0].content


async def test_post_ack_loss_recovered_without_second_card(fixture):
    f = fixture
    f.channel.uncertain = True
    row = await create(f, request_key="review-1")
    assert row["state"] == "posting"
    assert len(f.channel.messages) == 1
    again = await create(f, request_key="review-1")
    assert again["id"] == row["id"] and again["state"] == "open"
    assert len(f.channel.messages) == 1


async def test_unknown_post_not_blindly_retried(fixture):
    f = fixture
    f.channel.send = AsyncMock(side_effect=TimeoutError())
    row = await create(f)
    await f.service.tick(now=row["created"])
    await create(f)
    assert f.channel.send.await_count == 1 and f.store.get(row["id"])["state"] == "posting"
    assert "not yet confirmed" in pending_report(f.store.rows())


async def test_dedup_and_key_identity(fixture):
    f = fixture
    results = await asyncio.gather(create(f), create(f), create(f))
    assert len({r["id"] for r in results}) == 1 and len(f.channel.messages) == 1
    row = await create(f, request_key="same-operation")
    with pytest.raises(ValueError, match="different ask"):
        await create(f, question="Delete everything?", request_key="same-operation")
    await f.service.react(f.bot, reaction(row))
    assert (await create(f, request_key="same-operation"))["state"] == "answered"


async def test_reminder_once_even_across_reopen_or_failed_send(fixture):
    f = fixture
    row = await create(f)
    f.owner.send.side_effect = TimeoutError("ack lost")
    now = row["remind_at"] + 1
    f.service.cfg["digest_hour"] = 23
    # Isolate reminder from the independently scheduled daily digest.
    f.service.store.claim_digest(
        str(OWNER), __import__("datetime").datetime.fromtimestamp(now, f.service.cfg["timezone"]).date().isoformat()
    )
    await f.service.tick(now)
    assert f.owner.send.await_count == 1
    f.store.close()
    f.service.store = f.store = AskStore(f.path)
    await f.service.tick(now + 1)
    assert f.owner.send.await_count == 1
    assert f.store.get(row["id"])["reminder_status"] == "failed"


async def test_digest_empty_silent_then_every_open_ask_once_per_day(fixture):
    f = fixture
    f.service.cfg["digest_hour"] = 0
    now = time.time()
    await f.service.tick(now)
    f.owner.send.assert_not_awaited()
    first = await create(f)
    second = await f.service.ask("second", "Choose the cover?", "Keep the current cover.")
    await f.service.tick(now + 1)
    f.owner.send.assert_awaited_once()
    report = f.owner.send.call_args.args[0]
    assert report.startswith("```") and first["message_id"] in report and second["url"] in report
    assert "sample" in report and "second" in report
    await f.service.tick(now + 2)
    f.owner.send.assert_awaited_once()


async def test_review_digest_shows_oldest_three_asks_with_omitted_count(fixture):
    f = fixture
    f.service.cfg["digest_hour"] = 0
    now = time.time()
    for i in range(5):
        row = await create(f, question=f"Review item {i}?")
        f.store.db.execute("UPDATE owner_asks SET created=? WHERE id=?", (now - (5 - i) * 60, row["id"]))
    # SQL store order remains newest first for its other consumers.
    assert f.store.rows()[0]["payload"]["question"] == "Review item 4?"
    await f.service.digest(now)
    f.owner.send.assert_awaited_once()
    text = f.owner.send.call_args.args[0]
    assert "Asks (5)" in text and "+ 2 more" in text
    assert text.index("Review item 0?") < text.index("Review item 1?") < text.index("Review item 2?")
    assert "Review item 3?" not in text and "Review item 4?" not in text


async def add_hitl(f, now, *, recipient=OWNER, source="engine", status="pending"):
    db = f.connector._hitl.db
    if source == "engine":
        await db.execute(
            "INSERT INTO hitl_pending (hitl_id,agent_id,tool_name,args_json,description,"
            "channel_id,message_id,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(now), "sample", "send_email", "{}", "private body excluded", str(CHANNEL), "3001", status, now),
        )
    else:
        await db.execute(
            "INSERT INTO hitl_mcp_pending VALUES (?,?,?,?,?,?,?,?)",
            (str(now), "second", "create_tool", str(CHANNEL), "3002", json.dumps([str(recipient)]), now, now + 1800),
        )
    await db.commit()


def add_goal(title="Review release", *, status="blocked_on_user", created_by=OWNER, description=""):
    return goals.create_goal(title, description, "sample", str(CHANNEL), str(created_by), status=status)


async def test_digest_all_three_sources_use_one_existing_claim(fixture):
    f = fixture
    f.service.cfg["digest_hour"] = 0
    now = time.time()
    await create(f)
    await add_hitl(f, now)
    await add_hitl(f, now + 0.1, source="mcp")
    add_goal()
    await f.service.tick(now + 1)
    f.owner.send.assert_awaited_once()
    text = f.owner.send.call_args.args[0]
    for expected in ("Asks (1)", "HITL approvals (2)", "Goals (1)", "send_email", "create_tool", "Review release"):
        assert expected in text
    assert "private body excluded" not in text
    assert "3001" in text and "3002" in text
    assert "file" not in f.owner.send.call_args.kwargs
    # Reopen the durable claim store and race two digest callers as two bots would.
    f.store.close()
    f.service.store = f.store = AskStore(f.path)
    await asyncio.gather(f.service.digest(now + 2), f.service.digest(now + 2))
    f.owner.send.assert_awaited_once()
    assert f.store.db.execute("SELECT count(*) FROM owner_ask_digests").fetchone()[0] == 1


@pytest.mark.parametrize("source", ["engine", "mcp", "goals"])
async def test_digest_sends_without_any_owner_ask(fixture, source):
    f = fixture
    f.service.cfg["digest_hour"] = 0
    now = time.time()
    if source == "goals":
        add_goal()
    else:
        await add_hitl(f, now, source=source)
    await f.service.tick(now)
    f.owner.send.assert_awaited_once()
    assert "Asks (" not in f.owner.send.call_args.args[0]


@pytest.mark.parametrize("source", ["engine", "mcp", "goals"])
async def test_digest_unavailable_source_is_visible_and_does_not_hide_others(fixture, source, monkeypatch):
    f = fixture
    f.service.cfg["digest_hour"] = 0
    await create(f)
    if source == "goals":
        monkeypatch.setattr(goals, "list_goals", Mock(side_effect=RuntimeError("sensitive exception detail")))
        expected = "Goals: unavailable"
    else:
        table = "hitl_pending" if source == "engine" else "hitl_mcp_pending"
        await f.connector._hitl.db.execute(f"DROP TABLE {table}")
        expected = "HITL approvals: unavailable"
    await f.service.tick()
    text = f.owner.send.call_args.args[0]
    assert expected in text and "Asks (1)" in text
    assert "sensitive exception detail" not in text


async def test_digest_unavailable_is_not_misreported_as_empty(fixture, monkeypatch):
    f = fixture
    f.service.cfg["digest_hour"] = 0
    monkeypatch.setattr(goals, "list_goals", Mock(side_effect=OSError()))
    await f.service.tick()
    f.owner.send.assert_awaited_once()
    assert "Goals: unavailable" in f.owner.send.call_args.args[0]


async def test_digest_no_ready_bot_does_not_consume_claim_and_failed_dm_is_not_retried(fixture):
    f = fixture
    f.service.cfg["digest_hour"] = 0
    add_goal()
    f.service.ready_accounts.clear()
    await f.service.tick()
    assert f.store.db.execute("SELECT count(*) FROM owner_ask_digests").fetchone()[0] == 0
    f.service.ready_accounts.add("example")
    f.owner.send.side_effect = RuntimeError("DM closed")
    await f.service.tick()
    await f.service.tick()
    f.owner.send.assert_awaited_once()
    assert f.store.db.execute("SELECT status FROM owner_ask_digests").fetchone()[0] == "failed"


async def test_digest_two_concurrent_first_senders_share_one_claim(fixture):
    f = fixture
    f.service.cfg["digest_hour"] = 0
    add_goal()
    await asyncio.gather(f.service.digest(time.time()), f.service.digest(time.time()))
    f.owner.send.assert_awaited_once()
    assert f.store.db.execute("SELECT count(*) FROM owner_ask_digests").fetchone()[0] == 1


async def test_digest_filters_resolved_expired_and_foreign_hitl(fixture):
    f = fixture
    now = time.time()
    await add_hitl(f, now - 1900)
    await add_hitl(f, now - 1, status="approved")
    await add_hitl(f, now, source="mcp", recipient=OTHER)
    await add_hitl(f, now - 1901, source="mcp")
    assert (await f.service.digest_sources(str(OWNER), now))["HITL approvals"] == []
    await add_hitl(f, now + 1)
    f.connector._hitl.approvers = {str(OTHER)}
    assert (await f.service.digest_sources(str(OWNER), now))["HITL approvals"] == []


async def test_digest_foreign_ask_recipient_cannot_see_operational_sources(fixture):
    f = fixture
    await add_hitl(f, time.time())
    add_goal()
    sources = await f.service.digest_sources(str(OTHER), time.time())
    assert sources == {"Asks": [], "HITL approvals": [], "Goals": []}


@pytest.mark.parametrize(
    "status,change,expected",
    [
        ("proposed", {}, "kickoff"),
        ("blocked_on_user", {}, "blocked"),
        ("done", {}, "verdict"),
        ("done", {"verdict": "reached"}, None),
        ("abandoned", {}, None),
        ("executing", {}, None),
        ("paused", {}, None),
        ("executing", {"turns_since_human": 30}, "check-in"),
        ("executing", {"nomination": True}, "nominees"),
    ],
)
async def test_digest_goal_waiting_states(fixture, status, change, expected):
    f = fixture
    goal = add_goal(status=status)
    for key, value in change.items():
        if key == "nomination":
            goals.add_nomination(goal["id"], "second", "Needs review")
        else:
            goals._get_db().execute(f"UPDATE goals SET {key}=? WHERE id=?", (value, goal["id"]))
            goals._get_db().commit()
    rows = (await f.service.digest_sources(str(OWNER), time.time()))["Goals"]
    assert len(rows) == bool(expected)
    if expected:
        assert rows[0]["text"].startswith(expected + ":")


async def test_digest_goal_recipient_and_connector_are_respected(fixture):
    f = fixture
    f.config["admin_users"]["discord"].append(str(OTHER))
    add_goal("Another owner's private goal", created_by=OTHER)
    goals.create_goal("Foreign connector", "", "sample", str(CHANNEL), str(OWNER), connector="telegram")
    assert (await f.service.digest_sources(str(OWNER), time.time()))["Goals"] == []


async def test_stale_notifies_once_no_reminder_or_approval(fixture):
    f = fixture
    row = await create(f)
    await f.service.tick(row["stale_at"])
    await asyncio.gather(*list(f.service.events.values()))
    await f.service.tick(row["stale_at"] + 1)
    assert f.store.get(row["id"])["answer"] is None
    f.manager.handle_message.assert_awaited_once()
    f.owner.send.assert_not_awaited()


async def test_agent_continuation_replays_same_event_after_interruption(fixture):
    f = fixture
    row = await create(f)
    await f.service.react(f.bot, reaction(row))
    started = asyncio.Event()

    async def slow(*args):
        started.set()
        await asyncio.Event().wait()

    f.manager.handle_message.side_effect = slow
    await f.service.tick()
    await started.wait()
    task = next(iter(f.service.events.values()))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert f.store.db.execute("SELECT state FROM owner_ask_events").fetchone()[0] == "pending"
    original = f.manager.handle_message.call_args.args[1].content
    f.store.close()
    f.service.store = f.store = AskStore(f.path)
    f.manager.handle_message.side_effect = None
    await drain_events(f)
    assert f.store.db.execute("SELECT state FROM owner_ask_events").fetchone()[0] == "pending"
    f.store.db.execute("UPDATE owner_ask_events SET next_attempt=0")
    await drain_events(f)
    assert f.manager.handle_message.call_args.args[1].content == original
    assert f.store.db.execute("SELECT state FROM owner_ask_events").fetchone()[0] == "done"


async def test_pending_privacy_and_complete_large_report(fixture):
    f = fixture
    row = await create(f)
    commands = {}
    f.bot.tree = SimpleNamespace(command=lambda **kw: lambda fn: commands.setdefault(kw["name"], fn))
    register_pending(f.bot)
    i = interaction(f, row, user=OTHER)
    await commands["pending"](i)
    assert i.followup.send.call_args.kwargs["ephemeral"] is True
    assert "Publish" not in i.followup.send.call_args.args[0]
    i = interaction(f, row)
    await commands["pending"](i)
    assert "Publish" in i.followup.send.call_args.args[0]
    rows = [dict(row, id=str(n), agent_id=f"agent{n}", message_id=str(n)) for n in range(80)]
    report = pending_report(rows)
    target = SimpleNamespace(send=AsyncMock())
    await send_report(target, report, ephemeral=True)
    file = target.send.call_args.kwargs["file"]
    assert file.fp.read().decode() == report and "agent79" in report


@pytest.mark.parametrize(
    "kwargs",
    [
        {"default": ""},
        {"options": ["A"]},
        {"options": ["A", "a"]},
        {"options": ["A", 3]},
        {"question": "x" * 401},
        {"default": None},
    ],
)
async def test_bad_asks_do_not_post(fixture, kwargs):
    with pytest.raises(ValueError):
        await create(fixture, **kwargs)
    assert not fixture.channel.messages and not fixture.store.rows()


@pytest.mark.parametrize(
    "cfg",
    [
        {"digest_hour": 24},
        {"remind_after": 0},
        {"stale_after": 1},
        {"remind_after": float("nan")},
        {"owner_id": str(OTHER)},
    ],
)
def test_bad_configuration(cfg):
    with pytest.raises(ValueError):
        settings({"waiting_on_you": cfg, "admin_users": {"discord": [str(OWNER)]}})


async def test_tool_real_loopback_path_and_auth(fixture, monkeypatch):
    import aiohttp

    f = fixture
    api = InternalAPI(f.manager)
    await api.start()
    monkeypatch.setenv("KBOTS_INTERNAL_API", api.env["KBOTS_INTERNAL_API"])
    monkeypatch.setenv("KBOTS_INTERNAL_TOKEN", api.token)
    try:
        ctx = ToolContext(agent_id="sample")  # Same lack of manager as the MCP subprocess.
        result = json.loads(await ask_owner(ctx, "Review this change?", "Keep it on a branch.", request_key="change-1"))
        assert result["state"] == "open"
        retry = json.loads(await ask_owner(ctx, "Review this change?", "Keep it on a branch.", request_key="change-1"))
        assert retry["id"] == result["id"] and len(f.channel.messages) == 1
        async with aiohttp.ClientSession() as session:
            async with session.post(api.env["KBOTS_INTERNAL_API"] + "/owner-ask", json={}) as response:
                assert response.status == 401
            async with session.post(
                api.env["KBOTS_INTERNAL_API"] + "/owner-ask",
                headers={"Authorization": "Bearer " + api.token},
                json={"agent_id": "sample", "question": "Q", "default": "Hold", "answer": "Yes"},
            ) as response:
                assert response.status == 400  # Internal tools cannot record a human answer.
    finally:
        await api.stop()


async def test_actual_discord_ingress_consumes_closed_card_not_generic_approval(fixture):
    from src.connectors.discord import DiscordBot

    f = fixture
    row = await create(f)
    f.bot._wake_on_reaction = AsyncMock(side_effect=AssertionError("generic approval wake"))
    await DiscordBot.on_raw_reaction_add(f.bot, reaction(row))
    await DiscordBot.on_raw_reaction_add(f.bot, reaction(row))
    assert f.store.get(row["id"])["state"] == "answered"
    f.bot._wake_on_reaction.assert_not_awaited()
    reply = SimpleNamespace(
        author=SimpleNamespace(id=OWNER, bot=False),
        reference=SimpleNamespace(message_id=row["message_id"]),
        channel=f.channel,
        guild=f.channel.guild,
        content="Changed my mind",
        add_reaction=AsyncMock(),
    )
    await DiscordBot.on_message(f.bot, reply)
    assert f.store.get(row["id"])["answer"] == "Yes"


async def test_reminder_claim_survives_crash_before_ack(fixture):
    f = fixture
    row = await create(f)
    now = row["remind_at"] + 1
    assert f.store.claim_reminder(row["id"], now)
    f.store.close()
    f.service.store = f.store = AskStore(f.path)
    # The uncertain reminder is deliberately not sent again on restart.
    f.service.cfg["digest_hour"] = 23
    from datetime import datetime

    f.store.claim_digest(str(OWNER), datetime.fromtimestamp(now, f.service.cfg["timezone"]).date().isoformat())
    await f.service.tick(now)
    f.owner.send.assert_not_awaited()
    assert f.store.get(row["id"])["reminder_status"] == "uncertain"


async def test_digest_timezone_date_and_restart(fixture):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    f = fixture
    row = await create(f)
    f.service.cfg.update(timezone=ZoneInfo("Europe/Stockholm"), digest_hour=8)
    before = datetime(2030, 1, 2, 6, 59, tzinfo=timezone.utc).timestamp()
    f.store.db.execute(
        "UPDATE owner_asks SET created=?,remind_at=?,stale_at=?", (before, before + 86400, before + 604800)
    )
    await f.service.tick(before)
    f.owner.send.assert_not_awaited()
    await f.service.tick(before + 60)
    f.owner.send.assert_awaited_once()
    f.store.close()
    f.service.store = f.store = AskStore(f.path)
    await f.service.tick(before + 120)
    f.owner.send.assert_awaited_once()
    assert f.store.get(row["id"])["state"] == "open"
    assert f.store.db.execute("SELECT day FROM owner_ask_digests").fetchone()[0] == "2030-01-02"


async def test_card_edit_failure_remains_durable_and_retries(fixture):
    f = fixture
    row = await create(f)
    message = f.channel.messages[0]
    real_edit = message.edit
    message.edit = AsyncMock(side_effect=RuntimeError())
    await f.service.react(f.bot, reaction(row))
    assert f.store.get(row["id"])["dirty"] == 1
    assert f.store.get(row["id"])["state"] == "answered"
    message.edit = real_edit
    await drain_events(f)
    assert f.store.get(row["id"])["dirty"] == 0 and len(f.channel.messages) == 1


async def test_recovery_does_not_adopt_changed_card_with_matching_marker(fixture):
    f = fixture
    f.channel.uncertain = True
    row = await create(f)
    f.channel.messages[0].embeds[0].description = "A different, broader decision"
    await f.service.publish(row["id"])
    assert f.store.get(row["id"])["state"] == "posting"
    assert len(f.channel.messages) == 1


async def test_dm_card_and_reminder_have_sdk_enforced_dedup_nonces(fixture):
    from discord.http import handle_message_parameters

    f = fixture
    original_send = f.channel.send
    f.channel.send = AsyncMock(side_effect=original_send)
    row = await create(f)
    nonce = f.channel.send.call_args.kwargs["nonce"]
    assert nonce == row["id"][:24]
    with handle_message_parameters(content="test", nonce=nonce) as params:
        assert params.payload["nonce"] == nonce and params.payload["enforce_nonce"] is True
    await f.service.dm(row, "One reminder", "reminder:" + row["id"])
    assert len(f.owner.send.call_args.kwargs["nonce"]) <= 25


async def test_large_answer_and_maximum_payload_fit_plain_card(fixture):
    from src.connectors.discord_owner_asks import card

    f = fixture
    row = await create(f, question="q" * 400, default="d" * 300, context="c" * 400)
    row.update(state="answered", answer="a" * 400, answered_by=str(OWNER))
    embed, plain = card(row)
    assert len(plain.encode("utf-16-le")) // 2 <= 2000
    assert row["id"] not in plain and row["id"] not in embed.footer.text
    assert "If no reply: " + row["payload"]["default"] in plain


async def test_atomic_answer_and_outbox_rollback(fixture):
    import sqlite3

    f = fixture
    row = await create(f)
    f.store.db.execute(
        "CREATE TRIGGER deny_event BEFORE INSERT ON owner_ask_events BEGIN SELECT RAISE(ABORT,'fixture'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        f.store.finish(row["id"], answer="Yes", actor=str(OWNER))
    assert f.store.get(row["id"])["state"] == "open"
    assert f.store.get(row["id"])["answer"] is None
    assert not f.store.db.in_transaction


async def test_store_two_connections_dedup_and_one_reminder_claim(fixture):
    from src.core.owner_asks import validate

    f = fixture
    row = await create(f, request_key="shared")
    second = AskStore(f.path)
    try:
        same = second.create(
            agent_id=row["agent_id"],
            recipient_id=row["recipient_id"],
            account=row["account"],
            channel_id=row["channel_id"],
            guild_id=row["guild_id"],
            bot_id=row["bot_id"],
            payload=validate(row["payload"]["question"], row["payload"]["default"], request_key="shared"),
            cfg=f.service.cfg,
        )
        assert same["id"] == row["id"]
        assert f.store.claim_reminder(row["id"], row["remind_at"])
        assert not second.claim_reminder(row["id"], row["remind_at"])
    finally:
        second.close()


async def test_real_manager_serializes_answer_without_extra_queue_message(fixture, tmp_path):
    from src.core.agent_manager import AgentManager

    f = fixture
    mgr = AgentManager(
        agent_configs={
            "sample": {
                "project_dir": str(tmp_path / "sample"),
                "routing": {"discord": {"account": "example", "home_channel": str(CHANNEL)}},
            }
        },
        connectors={"discord": SimpleNamespace(send=AsyncMock())},
        llm_providers={},
        memory_backends={},
    )
    mgr._goal_turn_allowed = AsyncMock(return_value=True)
    observed = []

    async def inner(agent_id, message):
        observed.append((mgr.active_turns, mgr.inflight_snapshot()))
        return MessageDelivery(True)

    mgr._handle_message_inner = AsyncMock(side_effect=inner)
    f.service.manager = mgr
    row = await create(f)
    key = mgr._session_key("sample", str(CHANNEL))
    lock = mgr._session_locks.setdefault(key, asyncio.Lock())
    await lock.acquire()
    await f.service.react(f.bot, reaction(row))
    await f.service.tick()
    await asyncio.sleep(0)
    mgr.connectors["discord"].send.assert_not_awaited()
    mgr._handle_message_inner.assert_not_awaited()
    lock.release()
    await asyncio.gather(*list(f.service.events.values()))
    mgr._handle_message_inner.assert_awaited_once()
    assert mgr._handle_message_inner.call_args.args[1].user_id == str(OWNER)
    assert mgr.active_turns == 0
    assert observed == [(1, [])]  # Durable ask replay replaces generic recovery.


async def test_digest_comes_from_the_primary_bot_not_the_alphabetical_first(fixture):
    f = fixture
    f.service.cfg["digest_hour"] = 0
    # A work bot that sorts first must not deliver a fleet-wide summary.
    for account in ("aardvark", "example"):
        f.service.ready_accounts.add(account)
    f.connector.config = {"accounts": {"example": {}, "aardvark": {}}}
    sent = []
    for account in ("aardvark",):
        client = SimpleNamespace(user=SimpleNamespace(id=BOT + 1), fetch_user=AsyncMock(return_value=f.owner))
        f.connector.bots[account] = SimpleNamespace(account_name=account, client=client, connector=f.connector)
    original = f.service.dm

    async def record(sender, *args, **kwargs):
        sent.append(sender["account"])
        return await original(sender, *args, **kwargs)

    f.service.dm = record
    add_goal()
    await f.service.tick(time.time())
    assert sent == ["example"]
    assert f.service.digest_accounts()[0] == "example"


async def test_digest_comes_from_the_primary_bot_even_when_an_ask_is_open(fixture):
    f = fixture
    f.service.cfg["digest_hour"] = 0
    # An open ask belongs to one agent; the digest still speaks for the fleet,
    # so the ask's own bot must not become the sender of everyone's summary.
    row = await create(f)
    f.store.db.execute("UPDATE owner_asks SET account='aardvark' WHERE id=?", (row["id"],))
    f.service.ready_accounts.add("aardvark")
    f.connector.config = {"accounts": {"example": {}, "aardvark": {}}}
    client = SimpleNamespace(user=SimpleNamespace(id=BOT + 1), fetch_user=AsyncMock(return_value=f.owner))
    f.connector.bots["aardvark"] = SimpleNamespace(account_name="aardvark", client=client, connector=f.connector)
    sent = []
    original = f.service.dm

    async def record(sender, *args, **kwargs):
        sent.append(sender["account"])
        return await original(sender, *args, **kwargs)

    f.service.dm = record
    await f.service.digest(time.time())
    assert sent == ["example"]


async def test_digest_rows_say_what_each_decision_is_about(fixture):
    f = fixture
    f.service.cfg["digest_hour"] = 0
    await create(f, question="Publish it?", context="The draft quotes a customer by name.")
    goal = add_goal(title="Review release")
    goals.update_goal(goal["id"], "sample", blocked_brief="Pick the pricing tier before the listing goes up.")
    await f.service.digest(time.time())
    text = f.owner.send.call_args.args[0]
    assert "The draft quotes a customer by name." in text
    assert "Pick the pricing tier before the listing goes up." in text
    # A line of prose inside the block must not break the width budget.
    assert max(len(line.encode("utf-16-le")) // 2 for line in text.split("```")[1].splitlines()) <= 68


async def test_digest_ask_without_context_summarises_the_silent_default(fixture):
    f = fixture
    f.service.cfg["digest_hour"] = 0
    await create(f, question="Publish it?", default="Keep the draft unpublished.")
    await f.service.digest(time.time())
    assert "if no reply: Keep the draft unpublished." in f.owner.send.call_args.args[0]
