"""Real local clones and durable stores exercise the human setup boundaries."""

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from extras.posthog.alerts import EVENTS, PostHogAdapter, alert_heading, issue_link, parse_event
from src.connectors.discord_alerts import DiscordAlerts
from src.core.alert_channels import AlertError, AlertStore
from src.core.alert_repositories import resolve_repository
from tests.test_alert_lifecycle import Harness, http_error
from tests.test_alert_repositories import REMOTE, repository


class MemoryChannel:
    def __init__(self, channel_id=201, *, guild=None, embed_links=True):
        self.id, self.guild = channel_id, guild
        self.recipient = SimpleNamespace(id=101)
        self.messages, self.edits = [], []
        self.embed_links = embed_links
        self.permissions_for = lambda member: SimpleNamespace(embed_links=embed_links)

    async def history(self, **kwargs):
        for message in reversed(self.messages):
            yield message

    @staticmethod
    def components(view):
        return [SimpleNamespace(to_dict=lambda value=value: value) for value in view.to_components()] if view else []

    async def send(self, content, **kwargs):
        assert kwargs["allowed_mentions"].everyone is False
        message = SimpleNamespace(
            id=700 + len(self.messages),
            content=content,
            author=SimpleNamespace(id=999),
            webhook_id=None,
            nonce=kwargs.get("nonce"),
            components=self.components(kwargs.get("view")),
            embeds=[kwargs["embed"]] if kwargs.get("embed") else [],
        )

        async def edit(*, content, embed, **options):
            assert options["allowed_mentions"].everyone is False
            self.edits.append(content)
            message.content, message.embeds = content, [embed] if embed else []
            message.components = self.components(options.get("view"))
            return message

        message.edit = edit
        self.messages.append(message)
        return message

    async def fetch_message(self, identifier):
        return next(m for m in self.messages if m.id == identifier)


def visible_text(message):
    parts = [message.content or ""]
    for embed in message.embeds:
        parts.extend([embed.title or "", embed.description or ""])
        parts.extend(f"{field.name}: {field.value}" for field in embed.fields)
        parts.append(embed.footer.text or "")
    return "\n".join(p for p in parts if p)


@pytest.fixture
def ux(tmp_path):
    names = ["secrets/posthog-api-key", "secrets/posthog-api-key-read", "secrets/posthog-api-key-sample"]
    vault = SimpleNamespace(list_keys=lambda: names, get=Mock(side_effect=AssertionError("no key reads before CREATE")))
    connector = SimpleNamespace(vault=vault, _agent_manager=None, _admin_users=["101"])
    alerts = DiscordAlerts(
        connector,
        {"repository_roots": [str(tmp_path)], "adapters": {"posthog": "extras.posthog.alerts:PostHogAdapter"}},
        tmp_path / "state",
    )
    guild = SimpleNamespace(id=301, name="Example server")
    bot = SimpleNamespace(account_name="one", client=SimpleNamespace(guilds=[guild], user=SimpleNamespace(id=999)))
    connector.bots = {"one": bot}
    source = alerts.store.begin("worker", "101", "one", "201")
    alerts.store.update(source["id"], config={"service": "posthog", "message_format": 2})
    value = SimpleNamespace(
        alerts=alerts,
        store=alerts.store,
        bot=bot,
        names=names,
        source_id=source["id"],
        channel=MemoryChannel(),
        root=tmp_path,
        vault=vault,
    )
    value.current = lambda: alerts.store.get(value.source_id)
    value.message = lambda text: SimpleNamespace(
        channel=value.channel, guild=None, author=SimpleNamespace(id=101, bot=False), content=text
    )
    yield value
    alerts.store.close()


async def test_conversational_setup_uses_url_names_and_defaults_without_reading_keys(ux):
    clone = repository(ux.root / "not-the-public-name")
    replies = []
    for text in (
        "Sample App",
        f"I do not know the path but here is the repo: {REMOTE}",
        "https://eu.posthog.com/project/123/home",
        "There is already a key in vault for posthog",
        "Look for posthog",
        "1",
        "yes",
        "no",
    ):
        assert await ux.alerts.on_message(ux.bot, ux.message(text))
        replies.append(ux.channel.messages[-1].content)
    config = ux.current()["config"]
    assert config["app"] == "sample-app" and config["repo"] == str(clone)
    assert config["api_key"] == ux.names[0] and config["triggers"] == list(EVENTS)
    assert ux.current()["guild_id"] == "301"
    assert "1. secrets/posthog-api-key" in replies[2]
    assert "Using server Example server" in replies[5]
    assert "Reply CREATE" in replies[-1] and ux.source_id not in replies[-1]
    assert not ux.store.db.execute("SELECT 1 FROM operations").fetchone()
    ux.vault.get.assert_not_called()


@pytest.mark.parametrize("answer", ["", "yes", "ALL", "created, reopened, spiking"])
async def test_events_default_to_all(ux, answer):
    ux.store.update(
        ux.source_id,
        guild_id="301",
        config={
            "service": "posthog",
            "app": "sample",
            "repo": "/repo",
            "host": "https://eu.posthog.com",
            "project": "123",
            "api_key": ux.names[0],
        },
    )
    await ux.alerts.answer(ux.current(), ux.bot, answer)
    assert ux.current()["config"]["triggers"] == list(EVENTS)


async def test_one_key_and_one_server_are_selected_and_announced(ux):
    ux.names[:] = [ux.names[0]]
    ux.store.update(ux.source_id, config={"service": "posthog", "app": "sample", "repo": "/repo"})
    reply = await ux.alerts.answer(
        ux.current(), ux.bot, "https://eu.posthog.com/project/123/settings?tab=alerts#section"
    )
    assert "Using the existing vault key" in reply and "Using server Example server" in reply
    assert "socket" not in reply and "Which server" not in reply
    ux.vault.get.assert_not_called()


@pytest.mark.parametrize("selection", ["1", "Example server", "301"])
async def test_multiple_servers_accept_number_name_or_id(ux, selection):
    ux.bot.client.guilds.append(SimpleNamespace(id=302, name="Other server"))
    ux.store.update(
        ux.source_id,
        config={
            "service": "posthog",
            "app": "sample",
            "repo": "/repo",
            "host": "https://eu.posthog.com",
            "project": "123",
            "api_key": ux.names[0],
        },
    )
    await ux.alerts.answer(ux.current(), ux.bot, selection)
    assert ux.current()["guild_id"] == "301"


@pytest.mark.parametrize(
    "url",
    [
        "https://eu.posthog.com/project/123/home",
        "https://us.posthog.com/project/123/",
        "https://eu.posthog.com/project/123/error_tracking/anything?x=y#section",
    ],
)
def test_project_url_accepts_any_page(url):
    assert PostHogAdapter.parse_project(url)["project"] == "123"


@pytest.mark.parametrize(
    "url,message",
    [
        ("https://eu.i.posthog.com/project/123/home", "ingestion host"),
        ("https://eu.posthog.com/project/nope/home", "numeric project ID"),
    ],
)
def test_project_errors_distinguish_host_from_project(url, message):
    with pytest.raises(AlertError, match=message):
        PostHogAdapter.parse_project(url)


@pytest.mark.parametrize("state", ["provisioning", "provisional", "paused", "active", "disabled", "deleting"])
async def test_non_drafts_never_capture_normal_dms(ux, state):
    ux.store.update(ux.source_id, state=state)
    assert not await ux.alerts.on_message(ux.bot, ux.message("Can we work on the app?"))
    assert not ux.channel.messages


async def test_expired_draft_notifies_once_and_releases_the_same_message(ux):
    ux.store.db.execute("UPDATE sources SET updated=? WHERE id=?", (time.time() - 1801, ux.source_id))
    assert not await ux.alerts.on_message(ux.bot, ux.message("Hello"))
    assert ux.current()["state"] == "disabled"
    assert not await ux.alerts.on_message(ux.bot, ux.message("Are you there?"))
    rows = ux.store.db.execute("SELECT * FROM lifecycle_notices").fetchall()
    assert len(rows) == 1 and "30 minutes" in rows[0]["text"]
    assert json.loads(rows[0]["context"])["dm_id"] == "201"
    assert not ux.store.db.execute("SELECT 1 FROM operations").fetchone()


async def test_busy_repository_lookup_releases_chat_and_does_not_start_second_draft(ux, monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()
    ux.store.update(ux.source_id, config={"service": "posthog", "app": "sample"})

    async def lookup(*args):
        entered.set()
        await finish.wait()
        return ux.root

    monkeypatch.setattr("src.connectors.discord_alerts.asyncio.to_thread", lookup)
    task = asyncio.create_task(ux.alerts.on_message(ux.bot, ux.message(REMOTE)))
    await entered.wait()
    try:
        assert not await ux.alerts.on_message(ux.bot, ux.message("How are you?"))
        assert ux.store.begin("worker", "101", "one", "201")["id"] == ux.source_id
    finally:
        finish.set()
        await task
    assert ux.current()["waiting"] == 1


async def test_unexpected_setup_failure_logs_stack_and_type_but_no_exception_value(ux, monkeypatch, caplog):
    token = "opaque-value-that-must-never-escape"

    async def broken(*args):
        raise RuntimeError(token)

    monkeypatch.setattr(ux.alerts, "answer", broken)
    assert await ux.alerts.on_message(ux.bot, ux.message("Sample app"))
    assert "internal setup error" in ux.channel.messages[-1].content
    assert "RuntimeError" in caplog.text and "Traceback" in caplog.text and "broken" in caplog.text
    assert token not in caplog.text + ux.channel.messages[-1].content
    assert ux.current()["waiting"] == 1


async def test_known_permission_failure_has_safe_actionable_reason(ux, monkeypatch, caplog):
    async def denied(*args):
        raise http_error(403, 50013)

    monkeypatch.setattr(ux.alerts, "answer", denied)
    await ux.alerts.on_message(ux.bot, ux.message("CREATE"))
    assert "manage channels and webhooks" in ux.channel.messages[-1].content
    assert "Forbidden" in caplog.text


def test_prose_repository_lookup_retains_ambiguity_and_containment(tmp_path):
    repository(tmp_path / "one")
    repository(tmp_path / "two")
    with pytest.raises(AlertError, match="Multiple local clones"):
        resolve_repository(f"The repository is <{REMOTE}>.", [tmp_path])
    with pytest.raises(AlertError, match="one repository URL"):
        resolve_repository(f"Use {REMOTE} or https://example.com/team/other", [tmp_path])


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("marker", ["drill", "unknown"])
def test_old_and_new_protocols_keep_exact_ownership_and_drill_meaning(tmp_path, modern, marker):
    h = Harness(tmp_path)
    try:
        source = h.source()
        if modern:
            source = h.store.update(source["id"], config={**source["config"], "message_format": 2})
        event, issue = str(uuid.uuid4()), str(uuid.uuid4())
        version = "KBOTS_ALERT_V2" if modern else "KBOTS_ALERT_V1"
        envelope = f"{version} {source['id']} {source['nonce']} {EVENTS['created']} {event} {issue}"
        text = (
            (
                alert_heading(source)
                + "\nExpected drill error\n"
                + issue_link(source, issue)
                + "\n||"
                + envelope
                + " "
                + marker
                + "||"
            )
            if modern
            else envelope
        )
        parsed = parse_event(source, text)
        assert parsed.get("drill") is (marker == "drill" if modern else None)
        assert parsed["event_id"] == event and parsed["issue_id"] == issue
        with pytest.raises(AlertError):
            parse_event(source, text.replace(source["nonce"], "wrong"))
        payload = h.adapter._destination_payload(source, "synthetic-webhook")
        old = {**source, "config": {k: v for k, v in source["config"].items() if k != "message_format"}}
        old_payload = h.adapter._destination_payload(old, "synthetic-webhook")
        assert old_payload["inputs"]["content"]["value"].startswith("KBOTS_ALERT_V1 ")
        if modern:
            assert payload != old_payload
            assert "event.properties.test == true" in payload["inputs"]["content"]["value"]
    finally:
        h.close()


async def test_activation_dm_survives_restart_and_lost_send_response(tmp_path):
    h = Harness(tmp_path)
    dm = MemoryChannel()
    h.bot.client.fetch_channel = AsyncMock(return_value=dm)
    try:
        source = h.source(state="provisional")
        source = h.store.get(source["id"])
        h.store.db.execute("UPDATE sources SET dm_id='201' WHERE id=?", (source["id"],))
        event = str(uuid.uuid5(uuid.UUID(source["id"]), source["nonce"]))
        h.store.receive(source, event_id=event, issue_id=str(uuid.uuid4()), kind=EVENTS["created"], message_id="600")
        receipt = h.store.claim()
        h.store.save_result(receipt, "Setup test diagnosis")
        receipt = h.store.ready()[0]
        h.store.delivered(receipt, "700")
        h.store.delivered(receipt, "700")
        assert h.store.get(source["id"])["state"] == "active"
        assert h.store.db.execute("SELECT count(*) FROM lifecycle_notices").fetchone()[0] == 1
        original = dm.send

        async def lost(*args, **kwargs):
            await original(*args, **kwargs)
            raise TimeoutError()

        dm.send = lost
        await h.lifecycle.deliver_notices()
        assert len(dm.messages) == 1 and "are active" in dm.messages[0].content
        assert "https://discord.com/channels/301/401" in dm.messages[0].content
        h.store.close()
        h.store = AlertStore(tmp_path)
        h.lifecycle.store = h.alerts.transport.store = h.store
        notice = h.store.db.execute("SELECT * FROM lifecycle_notices").fetchone()
        await h.lifecycle.deliver_notices(now=notice["available"])
        assert h.store.db.execute("SELECT state FROM lifecycle_notices").fetchone()[0] == "complete"
        assert len(dm.messages) == 1
    finally:
        h.store.close()


@pytest.mark.parametrize("embeds", [True, False])
async def test_status_is_one_message_through_queue_investigation_and_result(tmp_path, embeds):
    h = Harness(tmp_path)
    h.guild.me = SimpleNamespace(id=999)
    room = MemoryChannel(401, guild=h.guild, embed_links=embeds)
    h.bot.client.fetch_channel = AsyncMock(return_value=room)
    try:
        source = h.source()
        h.store.receive(
            source, event_id=str(uuid.uuid4()), issue_id=str(uuid.uuid4()), kind=EVENTS["created"], message_id="600"
        )
        pending = h.store.pending()[0]
        await h.alerts.transport.queued(source, pending)
        receipt = h.store.claim()
        h.store.annotate(receipt, issue_name="TypeError: sample", setup_test=False, drill=False)
        await h.alerts.transport.progress(source, receipt)
        await h.alerts.transport.progress(source, receipt, stage=1)
        h.store.save_result(receipt, "Inspect the supplied handler")
        ready = h.store.ready()[0]
        result = await h.alerts.transport.report(source, ready)
        await h.alerts.transport.queued(source, pending)  # Stale late callback cannot regress the result.
        assert len(room.messages) == 1 and result["id"] == str(room.messages[0].id)
        assert "TypeError: sample" in visible_text(room.messages[0]) and "Inspect the supplied handler" in visible_text(
            room.messages[0]
        )
        from src.connectors.alert_embeds import status_nonce

        assert room.messages[0].nonce == status_nonce(receipt)
        assert "[alert:" not in visible_text(room.messages[0])
        assert bool(room.messages[0].embeds) is embeds
        assert "12 diagnoses" not in visible_text(room.messages[0])
    finally:
        h.close()


async def test_commands_can_select_the_only_app_without_a_uuid(ux):
    ux.store.update(ux.source_id, state="active")
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=101, bot=False),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await ux.alerts.command(ux.bot, interaction, "status", "")
    assert "active" in interaction.followup.send.call_args.args[0]
    assert ux.source_id not in interaction.followup.send.call_args.args[0]


async def test_failed_setup_diagnosis_has_one_durable_reason_and_no_success(tmp_path):
    h = Harness(tmp_path)
    try:
        source = h.source(state="provisional")
        event = str(uuid.uuid5(uuid.UUID(source["id"]), source["nonce"]))
        h.store.receive(source, event_id=event, issue_id=str(uuid.uuid4()), kind=EVENTS["created"], message_id="600")
        receipt = h.store.claim()
        h.store.save_result(receipt, "Issue read denied by PostHog", success=False)
        ready = h.store.ready()[0]
        h.store.delivered(ready, "700")
        h.store.delivered(ready, "700")
        notices = h.store.db.execute("SELECT * FROM lifecycle_notices").fetchall()
        assert len(notices) == 1 and "Issue read denied" in notices[0]["text"]
        assert "are active" not in notices[0]["text"]
        assert h.store.get(source["id"])["state"] == "provisional"
    finally:
        h.close()


async def test_superseded_success_notice_does_not_claim_monitoring_after_unsubscribe(tmp_path):
    h = Harness(tmp_path)
    try:
        source = h.source()
        h.store.notify_setup(source, "Alerts are active", "active")
        h.store.disable(source["id"])
        await h.lifecycle.deliver_notices()
        assert h.store.db.execute("SELECT state FROM lifecycle_notices").fetchone()[0] == "cancelled"
        h.bot.client.fetch_channel.assert_not_awaited()
    finally:
        h.close()


async def test_setup_notice_refuses_wrong_dm_recipient(tmp_path):
    h = Harness(tmp_path)
    try:
        source = h.source()
        h.store.db.execute("UPDATE sources SET dm_id='201' WHERE id=?", (source["id"],))
        source = h.store.get(source["id"])
        h.store.notify_setup(source, "Alerts active", "active")
        wrong = MemoryChannel()
        wrong.recipient.id = 202
        h.bot.client.fetch_channel = AsyncMock(return_value=wrong)
        await h.lifecycle.deliver_notices()
        assert not wrong.messages
        assert h.store.db.execute("SELECT state FROM lifecycle_notices").fetchone()[0] == "pending"
    finally:
        h.close()


async def test_lost_status_send_response_is_recovered_and_edited_without_new_send(tmp_path):
    h = Harness(tmp_path)
    h.guild.me = SimpleNamespace(id=999)
    room = MemoryChannel(401, guild=h.guild)
    h.bot.client.fetch_channel = AsyncMock(return_value=room)
    try:
        source = h.source()
        h.store.receive(
            source, event_id=str(uuid.uuid4()), issue_id=str(uuid.uuid4()), kind=EVENTS["created"], message_id="600"
        )
        pending = h.store.pending()[0]
        original = room.send

        async def lost(*args, **kwargs):
            await original(*args, **kwargs)
            raise TimeoutError()

        room.send = lost
        with pytest.raises(TimeoutError):
            await h.alerts.transport.queued(source, pending)
        receipt = h.store.claim()
        await h.alerts.transport.progress(source, receipt)
        assert len(room.messages) == 1 and "Investigating" in visible_text(room.messages[0])
        h.store.save_result(receipt, "Done")
        await h.alerts.transport.report(source, h.store.ready()[0])
        assert len(room.messages) == 1 and "Done" in visible_text(room.messages[0])
    finally:
        h.close()


def test_existing_database_migration_preserves_registration_and_resets_interrupted_draft(tmp_path):
    store = AlertStore(tmp_path)
    source = store.begin("worker", "101", "one", "201")
    store.update(source["id"], config={"service": "posthog"})
    store.db.execute("ALTER TABLE sources DROP COLUMN waiting")
    store.db.execute("ALTER TABLE receipts DROP COLUMN presentation")
    store.close()
    for _ in range(2):
        store = AlertStore(tmp_path)
        current = store.get(source["id"])
        assert current["config"] == {"service": "posthog"} and current["nonce"] == source["nonce"]
        assert current["waiting"] == 1 and "message_format" not in current["config"]
        store.update(source["id"], waiting=0)
        store.close()


async def test_unrecognised_pasted_key_is_neither_stored_nor_repeated(ux):
    ux.store.update(
        ux.source_id,
        config={
            "service": "posthog",
            "app": "sample",
            "repo": "/repo",
            "project": "123",
            "host": "https://eu.posthog.com",
        },
    )
    key = "synthetic-opaque-secret-value"
    reply = await ux.alerts.answer(ux.current(), ux.bot, key)
    assert key not in reply + "\n".join(ux.store.db.iterdump())
    assert "api_key" not in ux.current()["config"]
    ux.vault.get.assert_not_called()


@pytest.mark.parametrize("drill", [False, True])
async def test_drill_marker_survives_restart_and_title_alone_does_not_classify(tmp_path, monkeypatch, drill):
    from src.core import alert_diagnosis
    from src.core.base import LLMResponse

    h = Harness(tmp_path)
    try:
        source = h.source()
        source = h.store.update(source["id"], config={**source["config"], "repo": str(tmp_path)})
        h.store.receive(
            source,
            event_id=str(uuid.uuid4()),
            issue_id=str(uuid.uuid4()),
            kind=EVENTS["created"],
            message_id="600",
            drill=drill,
        )
        h.store.close()
        h.store = AlertStore(tmp_path)
        provider = SimpleNamespace(
            supports_tool_free=True, complete=AsyncMock(return_value=LLMResponse(content="Observed error"))
        )
        manager = SimpleNamespace(
            agent_configs={"worker": {"llm": {"model": "test"}}},
            defaults={},
            storage=None,
            _apply_provider_override=lambda *args: None,
            _get_agent_llm=lambda *args: provider,
            _effective_model=lambda *args: "test",
            active_turns=0,
        )
        monkeypatch.setattr(alert_diagnosis, "source_evidence", lambda *args: {"revision": "test", "snippets": []})
        monkeypatch.setattr(
            alert_diagnosis.AlertRepository,
            "fetch",
            lambda self, source, issue=None: {
                "repo": source["config"]["repo"],
                "revision": "HEAD",
                "selection": "offline fixture",
            },
        )
        adapter = SimpleNamespace(issue=AsyncMock(return_value={"name": "Deliberate test error"}))
        worker = alert_diagnosis.AlertWorker(
            h.store,
            {"posthog": adapter},
            manager,
            SimpleNamespace(progress=AsyncMock(), report=AsyncMock()),
            str(tmp_path),
        )
        await worker.once()
        issue = json.loads(provider.complete.call_args.args[0][1].content)["issue"]
        assert ("Deliberate drill:" in issue["alert_context"]) == drill
        assert "test" not in issue and "setup_test" not in issue
        assert h.store.ready()[0]["result"].startswith("Deliberate drill.") == drill
        assert provider.complete.call_args.kwargs["tools"] is None
    finally:
        h.store.close()


async def test_rate_limit_sentence_appears_only_after_real_budget_deferral(tmp_path):
    h = Harness(tmp_path)
    room = MemoryChannel(401, guild=h.guild)
    h.bot.client.fetch_channel = AsyncMock(return_value=room)
    try:
        source = h.source()
        h.store.receive(
            source, event_id=str(uuid.uuid4()), issue_id=str(uuid.uuid4()), kind=EVENTS["created"], message_id="600"
        )
        await h.alerts.transport.queued(source, h.store.pending()[0])
        assert "12 diagnoses" not in room.messages[0].content
        h.store.db.execute(
            "INSERT INTO budgets VALUES(?,?,12)", ("diagnosis:" + source["id"], int(time.time() // 3600))
        )
        assert h.store.claim() is None
        await h.alerts.transport.queued(source, h.store.pending()[0])
        assert "Queued until" in visible_text(room.messages[0]) and "12 diagnoses" in visible_text(room.messages[0])
        assert len(room.messages) == 1
    finally:
        h.close()


@pytest.mark.parametrize("answer,expected", [("yes", True), ("no", False)])
async def test_auto_fix_choice_is_required_and_visible_in_create_summary(ux, answer, expected):
    clone = repository(ux.root / "clone")
    ux.store.update(
        ux.source_id,
        guild_id="301",
        config={
            "service": "posthog",
            "app": "sample",
            "repo": str(clone),
            "project": "123",
            "host": "https://eu.posthog.com",
            "api_key": ux.names[0],
            "triggers": ["created"],
        },
    )
    assert "Open a fix PR automatically" in ux.alerts.question(ux.current(), ux.bot)
    with pytest.raises(AlertError, match="yes or no"):
        await ux.alerts.answer(ux.current(), ux.bot, "CREATE")
    assert ux.current()["state"] == "draft"
    reply = await ux.alerts.answer(ux.current(), ux.bot, answer)
    assert ux.current()["config"]["auto_fix_pr"] is expected
    assert "Automatic fix PRs: " + ("yes" if expected else "no, use Fix it") in reply
    assert "Reply CREATE" in reply
    ux.vault.get.assert_not_called()
