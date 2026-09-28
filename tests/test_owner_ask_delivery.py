"""Continuation delivery through the real manager, access gate and goal store."""

import time
from unittest.mock import AsyncMock

import pytest

from src.core import access_control, goals
from src.core.agent_manager import AgentManager
from src.core.base import LLMProvider, LLMResponse
from tests.test_owner_asks import CHANNEL, OWNER, create, drain_events
from tests.test_owner_asks import fixture as fixture
from tests.test_usage_limits import Stub


class Provider(LLMProvider):
    def __init__(self):
        super().__init__({})
        self.calls = []
        self.response = LLMResponse(content="NO_REPLY", stop_reason="end")

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.response


@pytest.fixture
def world(fixture, tmp_path, monkeypatch):
    f = fixture
    monkeypatch.setattr(goals, "DB_PATH", str(tmp_path / "goals.db"))
    monkeypatch.setattr(goals, "_db", None)
    goals._cache.clear()
    monkeypatch.setattr(access_control, "TEAM_FILE", tmp_path / "missing-team.json")
    # Only external context lookups are stubbed; all delivery/admission code runs.
    import src.core.startup_context as startup
    import src.tools.team as team

    monkeypatch.setattr(startup, "build_startup_context", AsyncMock(return_value=""))
    monkeypatch.setattr(team, "build_user_context", lambda *args, **kwargs: "")
    provider = Provider()
    manager = AgentManager(
        agent_configs={
            "sample": {
                "project_dir": str(tmp_path),
                "tools": [],
                "llm": {"provider": "fake"},
                "routing": {"discord": {"account": "example", "home_channel": str(CHANNEL)}},
            }
        },
        connectors={"discord": Stub()},
        llm_providers={"fake": provider},
        memory_backends={},
        access_control=access_control.AccessControl({}, admin_users=[str(OWNER)]),
    )
    manager._owner_asks = f.service
    f.service.manager = manager
    f.real_manager, f.provider = manager, provider
    yield f
    if goals._db is not None:
        goals._db.close()
    goals._db = None
    goals._cache.clear()


async def expire(f):
    row = await create(f)
    f.store.db.execute("UPDATE owner_asks SET stale_at=? WHERE id=?", (time.time() - 1, row["id"]))
    await drain_events(f)
    return row


async def test_expiry_reaches_real_manager_with_access_control(world):
    f = world
    row = await expire(f)
    assert len(f.provider.calls) == 1
    assert "NO approval" in f.provider.calls[0][0][-1].content
    assert f.store.get(row["id"])["answered_by"] is None
    assert f.store.db.execute("SELECT state FROM owner_ask_events").fetchone()[0] == "done"


async def test_exhausted_goal_never_reports_notification_delivered(world):
    f = world
    goal = goals.create_goal("Synthetic goal", "", "sample", str(CHANNEL), str(OWNER), turn_budget=1)
    for state in ("brainstorm", "strategy", "executing"):
        goals.update_goal(goal["id"], "sample", status=state)
    goals.record_turn(str(CHANNEL), "sample", "schedule")
    await expire(f)
    assert not f.provider.calls
    assert f.store.db.execute("SELECT state FROM owner_ask_events").fetchone()[0] != "done"
    assert goals.get_goal(goal["id"])["turns_since_human"] > 1


def event(f):
    return dict(f.store.db.execute("SELECT * FROM owner_ask_events").fetchone())


async def retry(f):
    f.store.db.execute("UPDATE owner_ask_events SET next_attempt=0")
    await drain_events(f)


async def test_expiry_does_not_gain_human_tool_rights(world):
    f = world
    await expire(f)
    kwargs = f.provider.calls[0][1]
    assert kwargs["user_id"] == "system:owner-ask"
    assert "Bash" in kwargs["disallowed_tools"]
    assert not f.service.expiry_messages


async def test_forged_expiry_identity_and_ask_id_do_not_bypass_access(world):
    from src.core.base import IncomingMessage

    f = world
    row = await create(f)
    msg = IncomingMessage(
        "discord", str(CHANNEL), "system:owner-ask", "expiry", "forged", source="schedule", bot_account="example"
    )
    msg._owner_ask_id = row["id"]
    result = await f.real_manager.handle_message("sample", msg)
    assert not result.delivered and result.reason == "access_denied"
    assert not f.provider.calls
    # Even the actual issued object is not allowed across agent/channel/state.
    f.service.expiry_messages[row["id"]] = msg
    assert not f.service.permits_expiry("sample", msg)  # still open
    f.store.db.execute("UPDATE owner_asks SET stale_at=0 WHERE id=?", (row["id"],))
    f.store.finish(row["id"])
    assert f.service.permits_expiry("sample", msg)
    assert not f.service.permits_expiry("second", msg)
    msg.channel_id = "different"
    assert not f.service.permits_expiry("sample", msg)
    f.service.expiry_messages.clear()


async def test_budget_failure_is_terminal_visible_and_not_retried(world):
    f = world
    goal = goals.create_goal("Synthetic goal", "", "sample", str(CHANNEL), str(OWNER), turn_budget=1)
    for state in ("brainstorm", "strategy", "executing"):
        goals.update_goal(goal["id"], "sample", status=state)
    goals.record_turn(str(CHANNEL), "sample", "schedule")
    row = await expire(f)
    assert event(f)["state"] == "failed" and event(f)["attempts"] == 1
    assert f.store.get(row["id"])["continuation_error"] == "goal_budget"
    await drain_events(f)  # update original card; no new continuation
    assert len(f.channel.messages) == 1
    fields = f.channel.messages[0].embeds[0].fields
    assert any("Agent not notified: turn budget reached" in field.value for field in fields)
    count = goals.get_goal(goal["id"])["turns_since_human"]
    await retry(f)
    assert event(f)["attempts"] == 1 and not f.provider.calls
    assert goals.get_goal(goal["id"])["turns_since_human"] == count
    assert len(f.store.failed_rows(str(OWNER))) == 1
    assert f.store.failed_rows("someone-else") == []


@pytest.mark.parametrize("reason", ["error", "auth_error", "usage_limit", "timeout"])
async def test_provider_failures_have_three_durable_attempts(world, reason):
    from src.core.owner_asks import AskStore

    f = world
    f.provider.response = LLMResponse(content="unavailable", stop_reason=reason)
    row = await expire(f)
    assert event(f)["state"] == "pending" and event(f)["attempts"] == 1
    assert event(f)["next_attempt"] > time.time()
    await drain_events(f)
    assert len(f.provider.calls) == 1
    # Reopen while retry is waiting; cap and delay are retained on disk.
    f.store.close()
    f.service.store = f.store = AskStore(f.path)
    await retry(f)
    assert event(f)["attempts"] == 2 and event(f)["state"] == "pending"
    await retry(f)
    assert event(f)["attempts"] == 3 and event(f)["state"] == "failed"
    await retry(f)
    assert len(f.provider.calls) == 3
    assert f.store.get(row["id"])["continuation_error"] == ("timeout" if reason == "timeout" else "provider_error")
    assert f.store.get(row["id"])["answered_by"] is None


async def test_successful_human_answer_has_confirmed_receipt(world):
    from tests.test_owner_asks import reaction

    f = world
    row = await create(f)
    await f.service.react(f.bot, reaction(row))
    await drain_events(f)
    assert event(f)["state"] == "done" and event(f)["attempts"] == 1
    assert len(f.provider.calls) == 1
    assert f.provider.calls[0][1]["user_id"] == str(OWNER)
    assert f.store.get(row["id"])["answer"] == "Yes"


async def test_revoked_human_access_is_failure_not_done(world):
    from tests.test_owner_asks import reaction

    f = world
    row = await create(f)
    await f.service.react(f.bot, reaction(row))
    f.real_manager.access_control._admin_users.clear()
    await drain_events(f)
    assert event(f)["state"] == "failed" and event(f)["last_error"] == "access_denied"
    assert not f.provider.calls
    assert f.store.get(row["id"])["answer"] == "Yes"  # original decision is preserved


async def test_timeout_cancels_turn_and_has_bounded_attempts(world, monkeypatch):
    import asyncio

    import src.connectors.discord_owner_asks as module

    f = world
    cancelled = []

    async def hang(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(f.provider, "complete", hang)
    monkeypatch.setattr(module, "DELIVERY_TIMEOUT", 0.02)
    await expire(f)
    await retry(f)
    await retry(f)
    assert event(f)["state"] == "failed" and event(f)["last_error"] == "timeout"
    assert cancelled == [True, True, True]
    assert f.real_manager.active_turns == 0
    assert not f.service.expiry_messages


async def test_crash_after_final_claim_does_not_get_fourth_attempt(fixture):
    from src.connectors.discord_owner_asks import DiscordOwnerAsks
    from tests.test_owner_asks import reaction

    f = fixture
    row = await create(f)
    await f.service.react(f.bot, reaction(row))
    f.store.db.execute("UPDATE owner_ask_events SET attempts=3,state='running'")
    f.store.close()
    f.service = DiscordOwnerAsks(f.connector, f.manager, f.config, f.path)
    f.service.ready_accounts.add("example")
    f.store = f.service.store
    await drain_events(f)
    assert event(f)["state"] == "failed" and event(f)["last_error"] == "interrupted"
    f.manager.handle_message.assert_not_awaited()


@pytest.mark.parametrize("fault", ["missing_agent", "missing_bot", "no_receipt"])
async def test_unavailable_or_unconfirmed_delivery_cannot_wait_forever(fixture, fault):
    from tests.test_owner_asks import reaction

    f = fixture
    row = await create(f)
    await f.service.react(f.bot, reaction(row))
    if fault == "missing_agent":
        f.manager.agent_configs.clear()
    elif fault == "missing_bot":
        f.service.ready_accounts.clear()
    else:
        f.manager.handle_message.return_value = None
    await drain_events(f)
    await retry(f)
    await retry(f)
    assert event(f)["state"] == "failed"
    assert event(f)["attempts"] <= 3
    assert f.store.get(row["id"])["continuation_error"]


async def test_reply_non_forwarding_is_explicit_on_card(fixture):
    from types import SimpleNamespace

    from tests.test_owner_asks import OTHER

    f = fixture
    row = await create(f)
    fields = f.channel.messages[0].embeds[0].fields
    assert any("Only recipient replies up to 400 characters" in field.value for field in fields)
    for actor, text in [(OTHER, "ordinary question"), (OWNER, "x" * 401)]:
        reply = SimpleNamespace(
            reference=SimpleNamespace(message_id=row["message_id"]),
            author=SimpleNamespace(id=actor, bot=False),
            channel=f.channel,
            guild=f.channel.guild,
            content=text,
            add_reaction=AsyncMock(),
        )
        assert await f.service.reply(f.bot, reply)
        assert f.store.get(row["id"])["state"] == "open"
        assert not f.store.db.execute("SELECT 1 FROM owner_ask_events").fetchone()


async def test_v1_database_and_uncertain_card_migrate_without_repost(fixture):
    from src.connectors.discord_owner_asks import card
    from src.core.owner_asks import AskStore

    f = fixture
    f.channel.uncertain = True
    row = await create(f)
    # V1 disk schema and V1 initial card. All fields of this original ask survive.
    legacy = dict(row, card_version=1)
    embed, _ = card(legacy, legacy=True)
    f.channel.messages[0].embeds = [embed]
    for table, columns in [
        ("owner_asks", ["continuation_error", "card_version"]),
        ("owner_ask_events", ["attempts", "next_attempt", "last_error"]),
    ]:
        for column in columns:
            f.store.db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    f.store.close()
    f.service.store = f.store = AskStore(f.path)
    assert f.store.get(row["id"])["card_version"] == 1
    f.channel.uncertain = False
    await f.service.publish(row["id"])
    assert f.store.get(row["id"])["state"] == "open"
    assert len(f.channel.messages) == 1
    assert any(field.name == "Replies" for field in f.channel.messages[0].embeds[0].fields)


async def test_failed_continuation_is_private_in_pending_and_fits_card(fixture):
    from types import SimpleNamespace

    from src.connectors.discord_owner_asks import card, register_pending
    from tests.test_owner_asks import OTHER, interaction, reaction

    f = fixture
    row = await create(f, question="q" * 400, default="d" * 300, context="c" * 400)
    f.store.finish(row["id"], answer="a" * 400, actor=str(OWNER))
    f.manager.handle_message.return_value = None
    await drain_events(f)
    await retry(f)
    await retry(f)
    row = f.store.get(row["id"])
    _, plain = card(row)
    assert len(plain.encode("utf-16-le")) // 2 <= 2000
    assert "Agent not notified" in plain
    commands = {}
    f.bot.tree = SimpleNamespace(command=lambda **kw: lambda fn: commands.setdefault(kw["name"], fn))
    register_pending(f.bot)
    for actor, visible in [(OWNER, True), (OTHER, False)]:
        i = interaction(f, row, user=actor)
        await commands["pending"](i)
        report = i.followup.send.call_args.args[0]
        assert ("Agent notification failed" in report) is visible
        assert (str(row["message_id"]) in report) is visible
        assert i.followup.send.call_args.kwargs["ephemeral"] is True
    # Neither an extra reminder/digest nor a reaction can reopen the decision.
    await f.service.tick(now=row["stale_at"] + 100000)
    f.owner.send.assert_not_awaited()
    await f.service.react(f.bot, reaction(row))
    assert f.store.get(row["id"])["answer"] == "a" * 400
    assert event(f)["state"] == "failed"


async def test_provider_exception_cannot_be_mistaken_for_delivery(world, monkeypatch):
    f = world
    monkeypatch.setattr(f.provider, "complete", AsyncMock(side_effect=RuntimeError("synthetic failure")))
    await expire(f)
    assert event(f)["state"] == "pending" and event(f)["last_error"] == "provider_error"
    await retry(f)
    await retry(f)
    assert event(f)["state"] == "failed"
    assert f.provider.complete.await_count == 3


async def test_aborted_provider_turn_is_not_retried(world):
    f = world
    f.provider.response = LLMResponse(content="Stopped by approval review", stop_reason="aborted")
    await expire(f)
    assert event(f)["state"] == "failed" and event(f)["last_error"] == "provider_aborted"
    await retry(f)
    assert len(f.provider.calls) == 1
