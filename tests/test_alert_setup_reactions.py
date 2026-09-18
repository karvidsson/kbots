"""Real setup transitions behind durable, exact-message reaction bindings."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.connectors.alert_setup_reactions import SetupReactions
from src.connectors.discord import DiscordBot
from src.core.alert_channels import AlertStore
from tests.test_alert_setup_ux import ux as ux_fixture

ux = ux_fixture


@pytest.fixture
def reactions(ux):
    o = ux
    o.store.update(
        o.source_id,
        guild_id="301",
        config={
            "service": "posthog",
            "message_format": 2,
            "app": "sample",
            "repo": "/repo",
            "host": "https://eu.posthog.com",
            "project": "123",
            "api_key": o.names[0],
            "triggers": ["created"],
        },
    )
    original_send = o.channel.send

    async def send(content, **kwargs):
        message = await original_send(content, **kwargs)
        message.channel = o.channel
        message.add_reaction = AsyncMock()
        original_edit = message.edit

        async def edit(**options):
            return await original_edit(embed=None, **options)

        message.edit = AsyncMock(side_effect=edit)
        return message

    o.channel.send = AsyncMock(side_effect=send)
    o.bot.client.fetch_channel = AsyncMock(return_value=o.channel)
    o.alerts.adapters["posthog"].check_credentials = AsyncMock()

    async def provision(source):
        o.store.update(source["id"], state="provisional")
        return "Checking alert delivery."

    o.alerts.provision = AsyncMock(side_effect=provision)
    o.payload = lambda message, emoji="✅", **kwargs: SimpleNamespace(
        **{
            "message_id": message.id,
            "channel_id": 201,
            "guild_id": None,
            "user_id": 101,
            "member": None,
            "emoji": emoji,
            **kwargs,
        }
    )
    return o


async def prompt(o):
    text = o.alerts.question(o.current(), o.bot)
    async with o.alerts.locks.setdefault(o.source_id, asyncio.Lock()):
        return await o.alerts.setup_reactions.send(o.bot, o.channel, o.current(), text)


@pytest.mark.parametrize("choice,expected", [("✅", True), ("🔴", False)])
async def test_reactions_choose_setting_then_create_once(reactions, choice, expected):
    o = reactions
    first = await prompt(o)
    assert [c.args[0] for c in first.add_reaction.await_args_list] == ["✅", "🔴"]
    assert "(yes/no)" in first.content
    assert not o.store.db.execute("SELECT 1 FROM operations").fetchone()
    assert await o.alerts.setup_reactions.react(o.bot, o.payload(first, choice))
    assert o.current()["config"]["auto_fix_pr"] is expected
    final = o.channel.messages[-1]
    assert "Reply CREATE" in final.content and "React ✅ to create or 🔴 to cancel" in final.content
    await asyncio.gather(*(o.alerts.setup_reactions.react(o.bot, o.payload(final)) for _ in range(2)))
    o.alerts.provision.assert_awaited_once()
    o.alerts.adapters["posthog"].check_credentials.assert_awaited_once()
    assert o.current()["state"] == "provisional"


async def test_final_negative_cancels_without_provisioning(reactions):
    o = reactions
    o.store.update(o.source_id, config={**o.current()["config"], "auto_fix_pr": False})
    message = await prompt(o)
    await o.alerts.setup_reactions.react(o.bot, o.payload(message, "🔴"))
    assert o.current()["state"] == "disabled"
    o.alerts.provision.assert_not_awaited()
    o.alerts.adapters["posthog"].check_credentials.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": 102},
        {"user_id": 999},
        {"member": SimpleNamespace(bot=True)},
        {"guild_id": 301},
        {"channel_id": 202},
        {"emoji": "👍"},
    ],
)
async def test_other_users_bots_and_wrong_context_cannot_answer(reactions, change):
    o = reactions
    message = await prompt(o)
    assert await o.alerts.setup_reactions.react(o.bot, o.payload(message, **change))
    assert "auto_fix_pr" not in o.current()["config"]
    o.bot.client.fetch_channel.assert_not_awaited()


@pytest.mark.parametrize("change", ["revision", "guild", "config", "account", "author", "webhook", "expired"])
async def test_stale_or_forged_binding_cannot_answer(reactions, change):
    o = reactions
    message = await prompt(o)
    if change == "revision":
        o.store.db.execute("UPDATE sources SET revision=revision+1 WHERE id=?", (o.source_id,))
    elif change == "guild":
        o.store.update(o.source_id, guild_id="302")
    elif change == "config":
        o.store.update(o.source_id, config={**o.current()["config"], "project": "456"})
    elif change == "account":
        o.bot.account_name = "two"
    elif change == "author":
        message.author.id = 102
    elif change == "webhook":
        message.webhook_id = 123
    elif change == "expired":
        o.store.db.execute("UPDATE sources SET updated=0 WHERE id=?", (o.source_id,))
    await o.alerts.setup_reactions.react(o.bot, o.payload(message))
    assert "auto_fix_pr" not in o.current()["config"]
    o.alerts.provision.assert_not_awaited()


async def test_typed_answer_invalidates_old_prompt_and_still_supports_create(reactions):
    o = reactions
    first = await prompt(o)
    await o.alerts.on_message(o.bot, o.message("no"))
    final = o.channel.messages[-1]
    await o.alerts.setup_reactions.react(o.bot, o.payload(first))
    assert o.current()["config"]["auto_fix_pr"] is False
    assert len(o.channel.messages) == 2
    await o.alerts.on_message(o.bot, o.message("CREATE"))
    await o.alerts.setup_reactions.react(o.bot, o.payload(final))
    o.alerts.provision.assert_awaited_once()


async def test_typed_cancel_remains_valid(reactions):
    o = reactions
    first = await prompt(o)
    await o.alerts.on_message(o.bot, o.message("CANCEL"))
    await o.alerts.setup_reactions.react(o.bot, o.payload(first))
    assert o.current()["state"] == "disabled"
    o.alerts.provision.assert_not_awaited()


async def test_binding_survives_database_reopen(reactions):
    o = reactions
    first = await prompt(o)
    directory = o.store.path.parent
    o.store.close()
    o.store = o.alerts.store = AlertStore(directory)
    o.alerts.setup_reactions = SetupReactions(o.alerts)
    o.alerts.operator.store = o.store
    await o.alerts.setup_reactions.react(o.bot, o.payload(first, "🔴"))
    assert o.current()["config"]["auto_fix_pr"] is False
    final = o.channel.messages[-1]
    await o.alerts.setup_reactions.react(o.bot, o.payload(final))
    o.alerts.provision.assert_awaited_once()


@pytest.mark.parametrize("phase", ["automatic", "confirm"])
@pytest.mark.parametrize("edit_fails", [False, True])
async def test_partial_seed_failure_keeps_typed_setup_and_explains_fallback(reactions, phase, edit_fails):
    o = reactions
    if phase == "confirm":
        o.store.update(o.source_id, config={**o.current()["config"], "auto_fix_pr": False})
    original_send = o.channel.send.side_effect

    async def send(content, **options):
        message = await original_send(content, **options)
        message.add_reaction.side_effect = [None, PermissionError("synthetic denial")]
        if edit_fails:
            message.edit.side_effect = PermissionError("synthetic denial")
        return message

    o.channel.send.side_effect = send
    first = await prompt(o)
    assert "Reaction controls are unavailable" in o.channel.messages[-1].content
    assert o.current()["waiting"] and o.current()["state"] == "draft"
    await o.alerts.setup_reactions.react(o.bot, o.payload(first))
    o.alerts.provision.assert_not_awaited()
    await o.alerts.on_message(o.bot, o.message("CREATE" if phase == "confirm" else "yes"))
    if phase == "confirm":
        o.alerts.provision.assert_awaited_once()
    else:
        assert o.current()["config"]["auto_fix_pr"] is True


async def test_gateway_dispatch_routes_setup_reactions_without_general_wake(reactions):
    o = reactions
    o.bot.connector = SimpleNamespace(_alerts=o.alerts)
    message = await prompt(o)
    await DiscordBot.on_raw_reaction_add(o.bot, o.payload(message, "🔴"))
    assert o.current()["config"]["auto_fix_pr"] is False
    # Unauthorized and stale reactions must also stop before general handlers.
    await DiscordBot.on_raw_reaction_add(o.bot, o.payload(message, user_id=102))


async def test_state_change_during_discord_read_refuses_reaction(reactions):
    o = reactions
    message = await prompt(o)

    async def fetch(identifier):
        o.store.disable(o.source_id)
        return o.channel

    o.bot.client.fetch_channel.side_effect = fetch
    await o.alerts.setup_reactions.react(o.bot, o.payload(message))
    assert "auto_fix_pr" not in o.current()["config"]
    o.alerts.provision.assert_not_awaited()


async def test_revocation_during_credential_check_cannot_revive_setup(reactions):
    o = reactions
    o.store.update(o.source_id, config={**o.current()["config"], "auto_fix_pr": False})
    message = await prompt(o)

    async def revoke(config):
        o.store.disable(o.source_id)

    o.alerts.adapters["posthog"].check_credentials.side_effect = revoke
    await o.alerts.setup_reactions.react(o.bot, o.payload(message))
    assert o.current()["state"] == "disabled"
    o.alerts.provision.assert_not_awaited()


async def test_failed_create_shows_full_summary_before_next_reaction(reactions):
    o = reactions
    o.store.update(o.source_id, config={**o.current()["config"], "auto_fix_pr": False})
    message = await prompt(o)
    o.alerts.adapters["posthog"].check_credentials.side_effect = PermissionError("fixture")
    await o.alerts.setup_reactions.react(o.bot, o.payload(message))
    assert "project 123" in o.channel.messages[-1].content and "Repository:" in o.channel.messages[-1].content
    assert o.channel.messages[-1].add_reaction.await_count == 2
    assert o.current()["state"] == "draft" and o.current()["waiting"]


async def test_reaction_during_seeding_waits_for_binding_and_falls_back_safely(reactions):
    o = reactions
    started, finish = asyncio.Event(), asyncio.Event()
    original_send = o.channel.send.side_effect

    async def send(content, **kwargs):
        message = await original_send(content, **kwargs)

        async def seed(emoji):
            started.set()
            await finish.wait()
            raise PermissionError("fixture")

        message.add_reaction.side_effect = seed
        return message

    o.channel.send.side_effect = send
    sending = asyncio.create_task(prompt(o))
    await started.wait()
    clicking = asyncio.create_task(o.alerts.setup_reactions.react(o.bot, o.payload(o.channel.messages[-1])))
    await asyncio.sleep(0)
    assert not clicking.done()
    finish.set()
    await sending
    await clicking
    assert "auto_fix_pr" not in o.current()["config"]


async def test_slash_resume_seeds_exact_original_response(reactions):
    o = reactions
    sent = []

    async def send(text, **kwargs):
        sent.append(await o.channel.send(text, **kwargs))

    interaction = SimpleNamespace(
        guild=None,
        user=SimpleNamespace(id=101, bot=False),
        channel_id=201,
        response=SimpleNamespace(send_message=AsyncMock(side_effect=send)),
        original_response=AsyncMock(side_effect=lambda: sent[-1]),
    )
    o.bot._resolve_agent = lambda interaction: "worker"
    await o.alerts.begin(o.bot, interaction, "posthog")
    assert sent[-1].add_reaction.await_count == 2
    await o.alerts.setup_reactions.react(o.bot, o.payload(sent[-1], "🔴"))
    assert o.current()["config"]["auto_fix_pr"] is False
