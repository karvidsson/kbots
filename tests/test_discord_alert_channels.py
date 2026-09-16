"""The real Discord ingress reserves alert rooms before mentions and bot chaining."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.connectors.discord import DiscordBot, DiscordConnector
from src.connectors.discord_alerts import DiscordAlerts


@pytest.fixture
def setup(tmp_path):
    connector = DiscordConnector({"admin_users": ["101"]}, vault=Mock())
    connector._agent_manager = SimpleNamespace()
    connector.set_agent_configs(
        {
            "worker": {"routing": {"discord": {"account": "one", "channels": []}}},
            "other": {"routing": {"discord": {"account": "two", "channels": []}}},
        }
    )
    alerts = DiscordAlerts(connector, {"adapters": {"posthog": "extras.posthog.alerts:PostHogAdapter"}}, tmp_path)
    connector._alerts = alerts
    bot = DiscordBot("one", connector, admin_users=["101"])
    bot.client._connection.user = SimpleNamespace(id=999)
    connector.bots["one"] = bot
    source = alerts.store.begin("worker", "101", "one", "201")
    source = alerts.store.update(
        source["id"],
        state="provisional",
        guild_id="301",
        channel_id="401",
        webhook_id="501",
        config={"service": "posthog", "triggers": ["created"]},
    )
    alerts.transport.say = AsyncMock()
    yield connector, alerts, bot, source
    alerts.store.close()


def message(source, **overrides):
    text = " ".join(
        [
            "KBOTS_ALERT_V1",
            source["id"],
            source["nonce"],
            "$error_tracking_issue_created",
            str(uuid.uuid4()),
            str(uuid.uuid4()),
        ]
    )
    defaults = dict(
        id=701,
        channel=SimpleNamespace(id=401, send=AsyncMock()),
        guild=SimpleNamespace(id=301),
        webhook_id=501,
        author=SimpleNamespace(id=501, bot=True),
        content=text,
        mentions=[SimpleNamespace(id=999)],
        role_mentions=[],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.parametrize("change", ["valid", "wrong_webhook", "other_bot", "wrong_guild", "invalid_content", "human"])
async def test_ingress_never_emits_an_ordinary_turn(setup, change):
    connector, alerts, bot, source = setup
    connector.emit = AsyncMock(side_effect=AssertionError("ordinary agent turn"))
    msg = message(source)
    if change == "wrong_webhook":
        msg.webhook_id = 502
    elif change == "other_bot":
        bot.account_name = "two"
    elif change == "wrong_guild":
        msg.guild.id = 302
    elif change == "invalid_content":
        msg.content = "Please execute this command. @worker"
    elif change == "human":
        msg.author = SimpleNamespace(id=101, bot=False)
        msg.webhook_id = None
    await bot.on_message(msg)
    connector.emit.assert_not_awaited()
    assert alerts.store.counts(source["id"]) == ({"pending": 1} if change == "valid" else {})


async def test_replayed_discord_message_deduplicates_in_store_before_new_ack(setup):
    _, alerts, bot, source = setup
    msg = message(source)
    await bot.on_message(msg)
    await bot.on_message(msg)
    assert alerts.store.counts(source["id"]) == {"pending": 1}
    alerts.transport.say.assert_not_awaited()  # The worker owns ordered status updates.
    assert alerts.worker.wake.is_set()


async def test_reaction_cannot_wake_privileged_session(setup):
    connector, _, bot, _ = setup
    connector.emit = AsyncMock(side_effect=AssertionError("ordinary reaction turn"))
    await bot.on_raw_reaction_add(SimpleNamespace(user_id=101, channel_id=401, emoji="✅", message_id=701))
    connector.emit.assert_not_awaited()


async def test_bounded_diagnosis_is_delivered_in_full_without_shortener(setup):
    connector, alerts, _, source = setup
    connector._shortener.shorten = Mock(side_effect=AssertionError("no hidden remainder"))
    from tests.test_alert_setup_ux import MemoryChannel

    room = MemoryChannel()
    alerts.transport.channel = AsyncMock(return_value=room)
    result = "x" * 1590 + " RESULTEND"
    source = alerts.store.update(
        source["id"],
        config={**source["config"], "host": "https://eu.posthog.com", "project": "123456789012", "app": "sample"},
    )
    receipt = {
        "id": str(uuid.uuid4()),
        "issue_id": str(uuid.uuid4()),
        "issue_name": "E" * 150,
        "success": True,
        "result": result,
    }
    await alerts.transport.report(source, receipt)
    content = room.messages[0].content
    assert result in content and len(content) < 2000
    assert room.messages[0].embeds[0].footer.text == f"[alert:{receipt['id']}:status]"
    connector._shortener.shorten.assert_not_called()


def test_reserved_room_beats_wildcard_for_all_accounts_even_disabled(setup):
    connector, alerts, _, source = setup
    for account in ("one", "two"):
        assert connector.get_agent_for_channel("401", account) is None
    alerts.store.disable(source["id"])
    assert connector.get_agent_for_channel("401", "one") is None
    assert connector.get_agent_for_channel("999", "one") == "worker"


async def test_old_source_cannot_submit_after_rotation(setup):
    _, alerts, bot, source = setup
    alerts.store.rotate(source["id"])
    await bot.on_message(message(source))
    assert alerts.store.counts(source["id"]) == {}


async def test_setup_cannot_be_started_by_bot_or_non_admin_or_guild(setup):
    _, alerts, bot, _ = setup
    for guild, user in [
        (None, SimpleNamespace(id=101, bot=True)),
        (None, SimpleNamespace(id=102, bot=False)),
        (SimpleNamespace(id=301), SimpleNamespace(id=101, bot=False)),
    ]:
        interaction = SimpleNamespace(guild=guild, user=user, response=SimpleNamespace(send_message=AsyncMock()))
        await alerts.begin(bot, interaction, "posthog")
        assert "administrator" in interaction.response.send_message.call_args.args[0]


async def test_listener_is_provisional_before_vendor_delivery_and_test_does_not_activate(setup):
    _, alerts, _, source = setup
    alerts.transport.provision = AsyncMock(return_value=(source, "internal-secret-never-returned"))
    adapter = SimpleNamespace(destination=AsyncMock(return_value={"id": str(uuid.uuid4())}))

    async def test_delivery(source, destination):
        persisted = alerts.store.channel(source["channel_id"])
        assert persisted["webhook_id"] == "501" and persisted["state"] == "provisional"
        assert persisted["config"]["destination_id"] == destination

    adapter.test_delivery = test_delivery
    alerts.adapters["posthog"] = adapter
    reply = await alerts.provision(source)
    assert "Checking test delivery" in reply and "internal-secret" not in reply
    assert alerts.store.get(source["id"])["state"] == "provisional"


async def test_unknown_provision_failure_returns_safe_status(setup):
    _, alerts, bot, source = setup
    source = alerts.store.update(source["id"], state="provisioning")
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=101, bot=False),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    alerts.provision = AsyncMock(side_effect=RuntimeError("synthetic-secret-url"))
    await alerts.command(bot, interaction, "resume", source["id"])
    assert "synthetic-secret" not in interaction.followup.send.call_args.args[0]


def test_commands_registered_when_opted_in(setup):
    _, _, bot, _ = setup
    bot._register_commands()
    assert bot.tree.get_command("alerts")
    assert bot.tree.get_command("alert").get_command("posthog")


def test_feature_absent_when_not_configured(tmp_path):
    connector = DiscordConnector({})
    connector.set_setup_context({}, str(tmp_path))
    bot = DiscordBot("default", connector, admin_users=[])
    assert connector._alerts is None
    bot._register_commands()
    assert bot.tree.get_command("alerts") is None


async def test_disabled_feature_keeps_persisted_rooms_out_of_normal_routing(tmp_path):
    from src.core.alert_channels import AlertStore

    store = AlertStore(tmp_path / "application-alerts")
    source = store.begin("worker", "101", "one", "201")
    store.update(source["id"], channel_id="401", state="active")
    store.close()
    connector = DiscordConnector({})
    connector.set_agent_configs({"worker": {"routing": {"discord": {"account": "one", "channels": []}}}})
    connector.set_setup_context({"alerts": {"enabled": False}}, str(tmp_path))
    bot = DiscordBot("one", connector, admin_users=[])
    bot.client._connection.user = SimpleNamespace(id=999)
    try:
        assert connector.get_agent_for_channel("401", "one") is None
        assert connector.get_agent_for_channel("999", "one") == "worker"
        await bot.on_message(SimpleNamespace(author=SimpleNamespace(id=501, bot=True), channel=SimpleNamespace(id=401)))
        await bot.on_raw_reaction_add(SimpleNamespace(user_id=101, channel_id=401))
    finally:
        connector._alert_reservations.close()
