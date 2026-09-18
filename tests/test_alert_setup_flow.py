"""One complete offline DM -> provisioning -> webhook -> diagnosis rehearsal."""

import copy
import json
import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from src.connectors.discord import DiscordBot, DiscordConnector
from src.connectors.discord_alerts import DiscordAlerts
from src.core.base import LLMResponse


@pytest.mark.parametrize("repository_input", ["path", "url"])
async def test_complete_setup_flow_requires_human_create_and_verified_test(tmp_path, monkeypatch, repository_input):
    from src.core import alert_diagnosis

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    remote = "https://code.example/team/sample.git"
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote], check=True)
    secrets = {"secrets/posthog-api-key": "synthetic-key-with-no-real-permissions"}
    vault = SimpleNamespace(get=secrets.get, set=lambda k, v: secrets.__setitem__(k, v), _fernet=object())
    connector = DiscordConnector({"admin_users": ["101"]}, vault=vault)
    connector.set_agent_configs({"worker": {"routing": {"discord": {"account": "one", "channels": []}}}})
    provider = SimpleNamespace(
        supports_tool_free=True,
        complete=AsyncMock(return_value=LLMResponse(content="Suspected null input; inspect handler.")),
    )
    connector._agent_manager = SimpleNamespace(
        agent_configs={"worker": {"llm": {"model": "test"}}},
        defaults={},
        storage=None,
        _apply_provider_override=lambda *a: None,
        _get_agent_llm=lambda *a: provider,
        _effective_model=lambda *a: "test",
        active_turns=0,
    )
    alerts = DiscordAlerts(
        connector,
        {"repository_roots": [str(tmp_path)], "adapters": {"posthog": "extras.posthog.alerts:PostHogAdapter"}},
        tmp_path / "state",
    )
    connector._alerts = alerts
    bot = DiscordBot("one", connector, admin_users=["101"])
    connector.bots["one"] = bot
    user = SimpleNamespace(id=999)
    guild = SimpleNamespace(id=301, name="Example server", default_role="everyone")
    room = Mock(spec=discord.TextChannel)
    room.id, room.guild, room.topic = 401, guild, None
    history, webhooks = [], []

    async def send(text=None, **kwargs):
        text = kwargs.pop("content", text)
        assert kwargs.get("allowed_mentions").everyone is False
        msg = SimpleNamespace(
            id=700 + len(history),
            author=user,
            webhook_id=None,
            content=text,
            embeds=[kwargs["embed"]] if kwargs.get("embed") is not None else [],
        )

        async def edit(*, content, embed, **kwargs):
            msg.content, msg.embeds = content, [embed] if embed else []
            return msg

        msg.edit = edit
        history.append(msg)
        return msg

    async def messages(**kwargs):
        for msg in reversed(history):
            yield msg

    async def create_room(name, **kwargs):
        assert name == "alerts-sample-app"
        assert kwargs["overwrites"]["everyone"].view_channel is False
        room.topic = kwargs["topic"]
        return room

    async def create_webhook(**kwargs):
        assert kwargs["name"] == "PostHog alerts"
        hook = SimpleNamespace(
            id=501,
            name=kwargs["name"],
            user=user,
            token="synthetic",
            url="https://discord.com/api/webhooks/501/synthetic-token",
        )
        webhooks.append(hook)
        return hook

    room.send, room.history = send, messages
    room.fetch_message = AsyncMock(side_effect=lambda ident: next(m for m in history if m.id == ident))
    room.webhooks, room.create_webhook = AsyncMock(return_value=webhooks), create_webhook
    guild.fetch_channels = AsyncMock(return_value=[])
    guild.fetch_member = AsyncMock(side_effect=lambda member_id: member_id)
    guild.create_text_channel = AsyncMock(side_effect=create_room)
    bot.client = SimpleNamespace(
        user=user, guilds=[guild], get_guild=lambda *a: guild, fetch_channel=AsyncMock(return_value=room)
    )
    interaction = SimpleNamespace(
        guild=None,
        user=SimpleNamespace(id=101, bot=False),
        channel_id=201,
        channel=SimpleNamespace(id=201),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    # Simulate the account startup reconciliation before accepting new setup.
    await alerts.lifecycle.reconcile("one")
    alerts.worker.accounts.add("one")
    await alerts.begin(bot, interaction, "posthog")
    source = alerts.store.draft("one", "101", "201")
    for text in (
        "Sample App",
        remote if repository_input == "url" else str(repo),
        "https://eu.posthog.com/project/123",
        "secrets/posthog-api-key",
        "created,reopened",
        "no",
    ):
        await alerts.answer(alerts.store.get(source["id"]), bot, text)
    source = alerts.store.get(source["id"])
    assert source["config"]["repo"] == str(repo.resolve())
    assert "Reply CREATE" in alerts.question(source, bot)
    guild.create_text_channel.assert_not_awaited()
    calls, remote = [], {}
    issue_id, destination_id = str(uuid.uuid4()), str(uuid.uuid4())

    async def request(config, method, path, **kwargs):
        calls.append((method, path))
        if path == "error_tracking/issues/?limit=1":
            return {"results": [{"id": issue_id}]}
        if path.startswith("hog_functions/?"):
            return {"results": [], "next": None}
        if method == "POST" and path == "hog_functions/":
            assert alerts.store.channel("401")["state"] == "provisional"
            remote.update(
                {
                    **copy.deepcopy(kwargs["payload"]),
                    "id": destination_id,
                    "hog": "print(inputs.content)",
                    "template": {"id": "template-discord", "code": "print(inputs.content)", "inputs_schema": []},
                }
            )
            # The serializer does not echo this write-only request field.
            remote.pop("template_id")
            return copy.deepcopy(remote)
        if method == "GET" and path == f"hog_functions/{destination_id}/":
            return copy.deepcopy(remote)
        if path.endswith("/invocations/"):
            payload = kwargs["payload"]
            # Pinned vendor endpoint rejects a request without configuration
            # unless use_draft=true; it does not infer configuration from the ID.
            assert payload["configuration"]["hog"] == remote["hog"]
            assert payload["configuration"]["inputs"] == remote["inputs"]
            assert payload["mock_async_functions"] is False
            event = payload["globals"]["event"]
            current = alerts.store.channel("401")
            text = f"KBOTS_ALERT_V1 {current['id']} {current['nonce']} "
            text += f"{event['event']} {event['uuid']} {event['distinct_id']}"
            if current["config"].get("message_format") == 2:
                from extras.posthog.alerts import alert_heading, issue_link

                text = (
                    alert_heading(current)
                    + "\nSetup delivery test\n"
                    + issue_link(current, issue_id)
                    + "\n||"
                    + text.replace("KBOTS_ALERT_V1", "KBOTS_ALERT_V2")
                    + " drill||"
                )
            await bot.on_message(
                SimpleNamespace(
                    id=600,
                    channel=room,
                    guild=guild,
                    webhook_id=501,
                    author=SimpleNamespace(id=501, bot=True),
                    content=text,
                )
            )
            return {"status": "success", "logs": ["must not be surfaced"]}
        if path == "error_tracking/query/issue_events/":
            from tests.test_alert_exception_evidence import response

            return response()
        if path == f"error_tracking/issues/{issue_id}/":
            return {"id": issue_id, "name": "TypeError in handler"}
        raise AssertionError((method, path))

    alerts.adapters["posthog"]._request = request
    monkeypatch.setattr(alert_diagnosis, "source_evidence", lambda *a: {"revision": "test", "snippets": []})
    monkeypatch.setattr(
        alert_diagnosis.AlertRepository,
        "fetch",
        lambda self, source, issue=None: {
            "repo": source["config"]["repo"],
            "revision": "HEAD",
            "selection": "offline fixture",
        },
    )
    try:
        reply = await alerts.answer(source, bot, "CREATE")
        assert "Checking test delivery" in reply
        assert alerts.store.counts(source["id"]) == {"pending": 1}
        await alerts.worker.once()
        assert alerts.store.get(source["id"])["state"] == "provisional"
        await alerts.worker.once()
        assert alerts.store.get(source["id"])["state"] == "active"
        assert alerts.store.counts(source["id"]) == {"complete": 1}
        assert len([call for call in calls if call[0] == "POST" and call[1].startswith("hog_functions/")]) == 2
        assert calls.count(("POST", "error_tracking/query/issue_events/")) == 2
        assert provider.complete.await_count == 1
        public = json.dumps([m.content for m in history]) + "\n".join(alerts.store.db.iterdump())
        assert "synthetic-token" not in public and "synthetic-key" not in public
        assert any("Setup check received. Alert path works." in m.content for m in history)
        assert all("Proposed fix for review" not in m.content for m in history)
        # A restarted reporter reconciles the receipt instead of posting twice.
        await alerts.worker.once()
        assert provider.complete.await_count == 1
    finally:
        alerts.store.close()


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Example App", "example-app"),
        ("  EXAMPLE   APP  ", "example-app"),
        ("example-app", "example-app"),
        ("\tMy\nNew App\t", "my-new-app"),
        ("My___App!!!", "my-app"),
        ("Café ÅÄÖ", "cafe-aao"),
        ("EXAM\u200bPLE APP", "example-app"),
        ("Ｆｕｌｌ Ｗｉｄｔｈ", "full-width"),
        ("1986 Console", "1986-console"),
        ("A", "a"),
        ("A" * 80, "a" * 41),
        ("A" * 40 + " End", "a" * 40),
        ("!!!", "app"),
        ("\u200b", "app"),
    ],
)
async def test_natural_app_name_normalized_before_creation_confirmation(tmp_path, name, expected):
    connector = SimpleNamespace(vault=SimpleNamespace(get=lambda _: None), _agent_manager=None)
    alerts = DiscordAlerts(
        connector, {"adapters": {"posthog": "extras.posthog.alerts:PostHogAdapter"}}, tmp_path / "state"
    )
    try:
        source = alerts.store.begin("worker", "101", "one", "201")
        source = alerts.store.update(
            source["id"],
            guild_id="301",
            config={
                "service": "posthog",
                "repo": str(tmp_path / "repo"),
                "project": "123",
                "host": "https://eu.posthog.com",
                "api_key": "secrets/service-key",
                "triggers": ["created"],
                "auto_fix_pr": False,
            },
        )
        assert "Spaces and capitals are fine" in alerts.question(source, None)
        response = await alerts.answer(source, None, name)
        saved = alerts.store.get(source["id"])
        assert saved["config"]["app"] == expected
        assert saved["state"] == "draft"
        assert response.startswith(f"Create alerts-{expected} in server ")
        assert "Reply CREATE to proceed" in response
        assert not alerts.store.db.execute("SELECT 1 FROM operations").fetchone()
    finally:
        alerts.store.close()


async def test_cancel_still_cancels_at_app_name_step(tmp_path):
    connector = SimpleNamespace(vault=SimpleNamespace(get=lambda _: None), _agent_manager=None)
    alerts = DiscordAlerts(connector, {}, tmp_path / "state")
    try:
        source = alerts.store.begin("worker", "101", "one", "201")
        assert "Setup stopped" in await alerts.answer(source, None, " CANCEL ")
        saved = alerts.store.get(source["id"])
        assert saved["state"] == "disabled" and "app" not in saved["config"]
    finally:
        alerts.store.close()
