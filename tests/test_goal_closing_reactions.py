"""Real gateway entry point and SQLite verdict store; Discord transport only is fake."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.connectors.discord import DiscordBot
from src.connectors.discord_goal_verdicts import handle_closing_reaction, recipient
from src.core import goals
from src.core.goal_notice import mark
from src.tools import goals as goal_tools


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(goals, "DB_PATH", str(tmp_path / "goals.db"))
    monkeypatch.setattr(goals, "_db", None)
    goals._cache.clear()
    goal = goals.create_goal("Ship a fix", "Description", "atlas", "300", "101", status="done")
    goal = goals.update_goal(goal["id"], "atlas", closing_message_id="400", summary="Full stored summary")
    goals.log_event(goal["id"], "atlas", "closed", "notice 400")
    guild = SimpleNamespace(id=200)
    author = SimpleNamespace(id=900, bot=True)
    channel = SimpleNamespace(id=300, guild=guild, delete=AsyncMock(), send=AsyncMock())
    card = SimpleNamespace(
        id=400,
        channel=channel,
        guild=guild,
        author=author,
        webhook_id=None,
        content=mark(goal["summary"]),
        edit=AsyncMock(),
    )
    channel.fetch_message = AsyncMock(return_value=card)
    user = SimpleNamespace(id=101, bot=False, system=False)
    dm = SimpleNamespace(id=500, recipient=user)
    receipt = SimpleNamespace(
        id=600,
        channel=dm,
        author=author,
        attachments=[SimpleNamespace(filename="goal-summary.txt", size=len(goal["summary"].encode()))],
        edit=AsyncMock(),
    )
    archive_bytes = []
    order = []

    async def send(*args, **kwargs):
        order.append("save")
        assert goals.get_goal(goal["id"])["verdict"] == "reached"
        archive_bytes.append(kwargs["file"].fp.read())
        if receipt.attachments:
            receipt.attachments[0].size = len(archive_bytes[-1])
        return receipt

    async def delete(**kwargs):
        order.append("delete")
        row = goals.verdict_delivery("400")
        assert row["receipt_message_id"] == "600" and row["state"] == "removing"

    dm.send = AsyncMock(side_effect=send)
    channel.delete.side_effect = delete
    user.create_dm = AsyncMock(return_value=dm)
    bot = DiscordBot.__new__(DiscordBot)
    bot.account_name = "atlas-account"
    bot.admin_users = ["101", "102"]
    bot.client = SimpleNamespace(
        user=author, fetch_user=AsyncMock(return_value=user), fetch_channel=AsyncMock(return_value=channel)
    )
    bot.connector = SimpleNamespace(
        _full_config={"connectors": {"discord": {"guild_id": "200"}}},
        _owner_asks=None,
        _alerts=None,
        _alert_reservations=None,
    )
    bot._wake_on_reaction = AsyncMock()
    monkeypatch.setattr("src.connectors.discord.DiscordConnector._reserved_alert", lambda *args: False)
    payload = SimpleNamespace(user_id=101, guild_id=200, channel_id=300, message_id=400, emoji="✅", member=user)
    yield SimpleNamespace(
        bot=bot,
        goal=goal,
        payload=payload,
        channel=channel,
        card=card,
        user=user,
        dm=dm,
        receipt=receipt,
        order=order,
        archive_bytes=archive_bytes,
    )
    if goals._db:
        goals._db.close()
    goals._db = None
    goals._cache.clear()


async def test_yes_via_gateway_saves_before_deleting_and_survives_reopen(world):
    w = world
    task = goals.add_task(w.goal["id"], "unfinished", "", "atlas", "atlas")
    await w.bot.on_raw_reaction_add(w.payload)
    assert w.order == ["save", "delete"]
    row = goals.get_goal(w.goal["id"])
    assert (row["verdict"], row["verdict_by"], row["status"]) == ("reached", "101", "done")
    assert goals.list_tasks(row["id"], statuses=("dropped",))[0]["id"] == task["id"]
    assert goals.list_tasks(row["id"], statuses=("dropped",))[0]["drop_reason"] == "goal closed"
    assert w.archive_bytes == [w.goal["summary"].encode()]
    goals._db.close()
    goals._db = None
    assert goals.verdict_delivery("400")["state"] == "complete"
    assert goals.verdict_delivery("400")["binding"] == (
        '{"account": "atlas-account", "bot_id": "900", "channel_id": "300", '
        '"guild_id": "200", "message_id": "400", "recipient_id": "101"}'
    )
    await w.bot.on_raw_reaction_add(w.payload)
    w.payload.emoji = "❌"
    await w.bot.on_raw_reaction_add(w.payload)
    assert w.order == ["save", "delete"]
    w.bot._wake_on_reaction.assert_not_awaited()
    report = await goal_tools.goal_status(SimpleNamespace(), row["id"])
    assert "Full stored summary" in report and "reached" in report
    assert "https://discord.com/channels/@me/500/600" in report


async def test_no_records_verdict_reopens_and_asks_in_room(world):
    w = world
    w.payload.emoji = "❌"
    goals.record_turn("300", "atlas", "bot")
    await w.bot.on_raw_reaction_add(w.payload)
    goal = goals.get_goal(w.goal["id"])
    assert goal["verdict"] == "not_reached" and goal["status"] == "executing"
    assert goal["verdict_by"] == "101" and goal["closing_message_id"] == ""
    assert "What is missing?" in w.card.edit.await_args.kwargs["content"]
    assert goals.goal_audience_for_channel("300")["owner"] == "atlas"
    assert goals.verdict_delivery("400")["state"] == "complete"
    w.channel.delete.assert_not_awaited()
    w.dm.send.assert_not_awaited()
    w.payload.emoji = "✅"
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(goal["id"])["verdict"] == "not_reached"
    assert w.card.edit.await_count == 1
    w.bot._wake_on_reaction.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        "other_admin",
        "other_user",
        "member_bot",
        "fetched_bot",
        "system_user",
        "member_id",
        "payload_channel",
        "payload_guild",
        "missing_guild",
        "fetched_channel",
        "fetched_guild",
        "message_guild",
        "message_channel",
        "message_id",
        "message_author",
        "human_author",
        "webhook",
        "payload_author",
        "changed_card",
        "unknown_emoji",
        "already_decided",
        "not_done",
        "superseded",
        "missing_summary",
        "missing_config_guild",
        "ambiguous_owner",
    ],
)
async def test_invalid_or_stale_card_never_decides_or_wakes(world, change):
    w = world
    if change in ("other_admin", "other_user"):
        w.payload.user_id = 102 if change == "other_admin" else 103
    elif change == "member_bot":
        w.payload.member = SimpleNamespace(id=101, bot=True)
    elif change == "fetched_bot":
        w.payload.member = None
        w.user.bot = True
    elif change == "system_user":
        w.user.system = True
    elif change == "member_id":
        w.payload.member = SimpleNamespace(id=103, bot=False)
    elif change == "payload_channel":
        w.payload.channel_id = 301
    elif change == "payload_guild":
        w.payload.guild_id = 201
    elif change == "missing_guild":
        del w.payload.guild_id
    elif change == "fetched_channel":
        w.channel.id = 301
    elif change == "fetched_guild":
        w.channel.guild = SimpleNamespace(id=201)
    elif change == "message_guild":
        w.card.guild = SimpleNamespace(id=201)
    elif change == "message_channel":
        w.card.channel = SimpleNamespace(id=301)
    elif change == "message_id":
        w.card.id = 401
    elif change == "message_author":
        w.card.author = SimpleNamespace(id=901, bot=True)
    elif change == "human_author":
        w.card.author = SimpleNamespace(id=900, bot=False)
    elif change == "webhook":
        w.card.webhook_id = 909
    elif change == "payload_author":
        w.payload.message_author_id = 901
    elif change == "changed_card":
        w.card.content += " tampered"
    elif change == "unknown_emoji":
        w.payload.emoji = "👍"
    elif change == "already_decided":
        goals.record_verdict(w.goal["id"], False, "101")
    elif change == "not_done":
        goals.update_goal(w.goal["id"], "atlas", status="executing")
    elif change == "superseded":
        goals.update_goal(w.goal["id"], "atlas", closing_message_id="401")
    elif change == "missing_summary":
        goals.update_goal(w.goal["id"], "atlas", summary="")
    elif change == "missing_config_guild":
        w.bot.connector._full_config = {}
    elif change == "ambiguous_owner":
        goals._get_db().execute("UPDATE goals SET created_by='atlas'")
        goals._get_db().commit()
    before = goals.get_goal(w.goal["id"])
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(w.goal["id"])["verdict"] == before["verdict"]
    assert goals.verdict_delivery("400") is None
    w.dm.send.assert_not_awaited()
    w.channel.delete.assert_not_awaited()
    w.card.edit.assert_not_awaited()
    w.bot._wake_on_reaction.assert_not_awaited()


@pytest.mark.parametrize("stage", ["fetch_user", "fetch_channel", "fetch_message"])
async def test_failed_binding_read_never_decides(world, stage):
    target = world.channel if stage == "fetch_message" else world.bot.client
    getattr(target, stage).side_effect = RuntimeError("transport unavailable")
    await world.bot.on_raw_reaction_add(world.payload)
    assert goals.get_goal(world.goal["id"])["verdict"] == ""
    world.channel.delete.assert_not_awaited()
    world.bot._wake_on_reaction.assert_not_awaited()


async def test_failed_removal_keeps_verdict_and_reports_in_room_and_archive(world):
    w = world
    w.channel.delete.side_effect = PermissionError("cannot delete")
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(w.goal["id"])["verdict"] == "reached"
    row = goals.verdict_delivery("400")
    assert row["state"] == "failed" and row["receipt_message_id"] == "600"
    assert "not confirmed" in w.card.edit.await_args.kwargs["content"]
    assert "not confirmed" in w.receipt.edit.await_args.kwargs["content"]
    await w.bot.on_raw_reaction_add(w.payload)
    assert w.channel.delete.await_count == 1


@pytest.mark.parametrize("failure", ["blocked_dm", "unconfirmed_send", "wrong_recipient", "wrong_bot_receipt"])
async def test_no_room_removal_without_confirmed_external_summary(world, failure):
    w = world
    if failure == "blocked_dm":
        w.dm.send.side_effect = PermissionError("blocked")
    elif failure == "unconfirmed_send":
        w.receipt.attachments = []
    elif failure == "wrong_recipient":
        w.dm.recipient = SimpleNamespace(id=102)
    else:
        w.receipt.author = SimpleNamespace(id=901)
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(w.goal["id"])["verdict"] == "reached"
    assert goals.verdict_delivery("400")["state"] == "failed"
    w.channel.delete.assert_not_awaited()
    assert "Verdict recorded" in w.card.edit.await_args.kwargs["content"]


@pytest.mark.parametrize("shared", ["anchored", "second_goal"])
async def test_shared_room_stays(world, shared):
    w = world
    if shared == "anchored":
        goals._get_db().execute("UPDATE goals SET anchored=1")
        goals._get_db().commit()
    else:
        goals.create_goal("Another goal", "", "beacon", "300", "101")
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(w.goal["id"])["verdict"] == "reached"
    assert "Shared room retained" in w.card.edit.await_args.kwargs["content"]
    w.channel.delete.assert_not_awaited()
    assert w.archive_bytes


async def test_full_summary_archived_even_when_visible_card_was_truncated(world):
    w = world
    summary = "Summary\n" + "details " * 900
    goals.update_goal(w.goal["id"], "atlas", summary=summary)
    w.card.content = mark(summary)[:1900]
    await w.bot.on_raw_reaction_add(w.payload)
    assert w.archive_bytes == [summary.encode()]
    assert w.order == ["save", "delete"]


async def test_concurrent_second_verdict_cannot_duplicate_side_effects(world):
    w = world
    entered, release = asyncio.Event(), asyncio.Event()
    send = w.dm.send.side_effect

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return await send(*args, **kwargs)

    w.dm.send.side_effect = slow
    first = asyncio.create_task(w.bot.on_raw_reaction_add(w.payload))
    await entered.wait()
    await w.bot.on_raw_reaction_add(w.payload)
    release.set()
    await first
    assert w.order == ["save", "delete"]


async def test_snapshot_changed_during_authentication_cannot_decide(world):
    w = world

    async def fetch(*args):
        goals.update_goal(w.goal["id"], "atlas", closing_message_id="401")
        return w.card

    w.channel.fetch_message.side_effect = fetch
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(w.goal["id"])["verdict"] == ""
    w.channel.delete.assert_not_awaited()


async def test_reopened_while_archiving_is_not_removed(world):
    w = world
    send = w.dm.send.side_effect

    async def reopen(*args, **kwargs):
        result = await send(*args, **kwargs)
        goals.update_goal(w.goal["id"], "atlas", status="executing")
        return result

    w.dm.send.side_effect = reopen
    await w.bot.on_raw_reaction_add(w.payload)
    w.channel.delete.assert_not_awaited()
    assert goals.verdict_delivery("400")["state"] == "failed"


async def test_card_edit_failure_has_visible_fallback(world):
    w = world
    w.payload.emoji = "❌"
    w.card.edit.side_effect = PermissionError("edit denied")
    await w.bot.on_raw_reaction_add(w.payload)
    assert "What is missing?" in w.channel.send.await_args.args[0]
    assert goals.verdict_delivery("400")["state"] == "complete"


async def test_unknown_card_does_not_claim_event(world):
    world.payload.message_id = 987
    assert await handle_closing_reaction(world.bot, world.payload) is False


@pytest.mark.parametrize(
    "created,config,admins,expected",
    [
        ("atlas", {}, ["101"], "101"),
        ("atlas", {}, ["101", "102"], ""),
        ("102", {}, ["101", "102"], "102"),
        ("103", {}, ["101"], ""),
        ("102", {"goals": {"escalation_user": "101"}}, ["101", "102"], "101"),
        ("atlas", {"waiting_on_you": {"owner_id": "101"}}, ["101", "102"], "101"),
        ("atlas", {"goals": {"escalation_user": "103"}}, ["101"], ""),
    ],
)
def test_entitlement_is_specific_not_any_admin(created, config, admins, expected):
    assert recipient({"created_by": created}, config, admins) == expected


async def test_declined_goal_can_finish_again_with_a_new_card(world, monkeypatch):
    w = world
    w.payload.emoji = "❌"
    await w.bot.on_raw_reaction_add(w.payload)
    monkeypatch.setattr(goal_tools, "_post_to_channel", AsyncMock(return_value="401"))
    monkeypatch.setattr(goal_tools, "_add_reactions", AsyncMock())
    row = goals.update_goal(w.goal["id"], "atlas", status="done")
    row, note = await goal_tools._close_goal(SimpleNamespace(agent_id="atlas"), row)
    assert row["closing_message_id"] == "401" and row["verdict"] == ""
    assert "awaiting" in note
    assert goals.is_closing_message("400")
    assert goals.verdict_delivery("400")["verdict"] == "not_reached"
    w.payload.emoji = "✅"
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(row["id"])["verdict"] == ""
    w.channel.delete.assert_not_awaited()
    w.payload.message_id = 401
    w.card.id, w.card.content = 401, mark(row["summary"])[:1900]
    w.channel.delete.side_effect = None  # fixture's assertion names the first card
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(row["id"])["verdict"] == "reached"
    assert goals.verdict_delivery("401")["state"] == "complete"
    assert len(goals.verdict_deliveries(row["id"])) == 2


async def test_negative_notice_failure_is_durable_and_never_removes_room(world):
    w = world
    w.payload.emoji = "❌"
    w.card.edit.side_effect = PermissionError("no edit")
    w.channel.send.side_effect = PermissionError("no send")
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(w.goal["id"])["verdict"] == "not_reached"
    assert goals.get_goal(w.goal["id"])["status"] == "executing"
    assert goals.verdict_delivery("400")["state"] == "failed"
    w.channel.delete.assert_not_awaited()
    assert "failed" in await goal_tools.goal_status(SimpleNamespace(), w.goal["id"])


async def test_cancellation_keeps_verdict_and_surfaces_pending_cleanup(world):
    w = world
    w.channel.delete.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(w.goal["id"])["verdict"] == "reached"
    assert goals.verdict_delivery("400")["state"] == "failed"
    assert "not confirmed" in w.card.edit.await_args.kwargs["content"]
    await w.bot.on_raw_reaction_add(w.payload)
    assert w.channel.delete.await_count == 1


async def test_interrupted_journal_is_visible_after_restart_and_not_replayed(world):
    w = world
    goals.record_verdict(w.goal["id"], True, "101", binding={"account": "atlas-account"})
    goals._db.close()
    goals._db = None
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.verdict_delivery("400")["state"] == "pending"
    report = await goal_tools.goal_status(SimpleNamespace(), w.goal["id"])
    assert "follow-up not confirmed" in report
    w.channel.delete.assert_not_awaited()
    w.dm.send.assert_not_awaited()


def test_additive_migration_does_not_rewrite_old_goals_or_events(world):
    db = goals._get_db()
    before_goals = [tuple(r) for r in db.execute("SELECT * FROM goals")]
    before_events = [tuple(r) for r in db.execute("SELECT * FROM goal_events")]
    db.execute("DROP TABLE goal_verdict_deliveries")
    db.commit()
    goals._ensure_schema(db)
    assert [tuple(r) for r in db.execute("SELECT * FROM goals")] == before_goals
    assert [tuple(r) for r in db.execute("SELECT * FROM goal_events")] == before_events
    assert goals.verdict_deliveries(world.goal["id"]) == []


async def test_different_connected_bot_cannot_decide(world):
    w = world
    w.bot.account_name = "beacon-account"
    w.bot.client.user = SimpleNamespace(id=901)
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(w.goal["id"])["verdict"] == ""
    w.dm.send.assert_not_awaited()


def test_shared_card_does_not_promise_room_removal(world):
    text = goal_tools._closing_text({**world.goal, "anchored": 1})
    assert "shared room stays open" in text
    assert "room is removed" not in text
    assert "Without an answer the room stays open" in text


def test_two_processes_only_claim_one_verdict(world):
    import subprocess
    import sys

    program = """
import sys
from src.core import goals
goals.DB_PATH = sys.argv[1]
goal = goals.get_goal(sys.argv[2])
print('ready', flush=True)
sys.stdin.readline()
result = goals.record_verdict(goal['id'], True, '101', binding={'account': 'atlas-account'})
print(bool(result), flush=True)
"""
    # Real separate SQLite connections, as used by the engine and MCP worker.
    children = [
        subprocess.Popen(
            [sys.executable, "-c", program, str(goals.db_path()), world.goal["id"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = [child.communicate(timeout=10) for child in children]
        assert all(child.returncode == 0 for child in children), results
        assert sorted(out.strip() for out, err in results) == ["False", "True"]
        assert len(goals.verdict_deliveries(world.goal["id"])) == 1
        assert goals._get_db().execute("SELECT COUNT(*) FROM goal_events WHERE kind='verdict'").fetchone()[0] == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.parametrize("interrupted", [False, True])
async def test_failed_final_archive_edit_cannot_erase_confirmed_removal(world, interrupted):
    w = world
    w.receipt.edit.side_effect = asyncio.CancelledError() if interrupted else PermissionError("no edit")
    if interrupted:
        with pytest.raises(asyncio.CancelledError):
            await w.bot.on_raw_reaction_add(w.payload)
    else:
        await w.bot.on_raw_reaction_add(w.payload)
    row = goals.verdict_delivery("400")
    assert row["state"] == "complete" and "room removed" in row["note"]
    assert row["receipt_message_id"] == "600" and "Final DM status update" in row["note"]
    assert w.order == ["save", "delete"]


@pytest.mark.parametrize("change", ["admins", "owner", "guild"])
async def test_authorization_revoked_during_discord_read_cannot_decide(world, change):
    w = world

    async def fetch(*args):
        if change == "admins":
            w.bot.admin_users = ["102"]
        elif change == "owner":
            w.bot.connector._full_config = {
                "connectors": {"discord": {"guild_id": "200"}},
                "goals": {"escalation_user": "102"},
            }
        else:
            w.bot.connector._full_config = {"connectors": {"discord": {"guild_id": "201"}}}
        return w.card

    w.channel.fetch_message.side_effect = fetch
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(w.goal["id"])["verdict"] == ""
    assert goals.verdict_delivery("400") is None
    w.channel.delete.assert_not_awaited()
    w.dm.send.assert_not_awaited()


@pytest.mark.parametrize("cut_whitespace", [" ", "\n"])
async def test_discord_trimmed_truncation_still_accepts_verdict(world, cut_whitespace):
    w = world
    summary = "A" * 1898 + cut_whitespace + "The rest must survive in the archive."
    goals.update_goal(w.goal["id"], "atlas", summary=summary)
    sent_content = mark(summary)[:1900]
    assert len(sent_content) == 1900 and sent_content[-1] == cut_whitespace
    w.card.content = sent_content.rstrip()  # Discord's returned message representation
    await w.bot.on_raw_reaction_add(w.payload)
    assert goals.get_goal(w.goal["id"])["verdict"] == "reached"
    assert w.order == ["save", "delete"]
    assert w.archive_bytes == [summary.encode()]


@pytest.mark.parametrize("persistent_db_failure", [False, True])
async def test_deleted_room_reported_even_when_completion_db_write_fails(
    world, monkeypatch, caplog, persistent_db_failure
):
    import sqlite3

    w = world
    update = goals.update_verdict_delivery

    def fail_completion(message_id, state, note, **kwargs):
        if w.channel.delete.await_count and (persistent_db_failure or state == "complete"):
            raise sqlite3.OperationalError("completion storage unavailable")
        return update(message_id, state, note, **kwargs)

    monkeypatch.setattr(goals, "update_verdict_delivery", fail_completion)
    await w.bot.on_raw_reaction_add(w.payload)
    assert w.order == ["save", "delete"]
    assert goals.get_goal(w.goal["id"])["verdict"] == "reached"
    notice = w.receipt.edit.await_args.kwargs["content"]
    assert "Removal reported by Discord" in notice and "completion record could not be updated" in notice
    assert "removal is not confirmed" not in notice and "Check the room" not in notice
    assert "Removal reported by Discord" in caplog.text
    w.card.edit.assert_not_awaited()  # the card's room is already gone
    w.channel.send.assert_not_awaited()
    row = goals.verdict_delivery("400")
    assert row["receipt_message_id"] == "600"
    if persistent_db_failure:
        assert row["state"] == "removing"  # outage also prevents persisting the failure note
    else:
        assert row["state"] == "failed" and "Removal reported by Discord" in row["note"]
    goals._db.close()
    goals._db = None
    await w.bot.on_raw_reaction_add(w.payload)
    assert w.channel.delete.await_count == 1 and w.dm.send.await_count == 1
