"""Deletion evidence, interrupted setup and durable cleanup at the real boundaries."""

import asyncio
import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from extras.posthog.alerts import DestinationNotFoundError, PostHogAdapter
from src.connectors.discord import DiscordBot, DiscordConnector
from src.connectors.discord_alerts import DiscordAlerts
from src.core.alert_channels import AlertError


class Harness:
    def __init__(self, directory):
        self.directory = directory
        self.secrets = {"secrets/service-key": "synthetic-key-never-used-on-network"}
        self.vault = SimpleNamespace(
            get=self.secrets.get,
            set=self.secrets.__setitem__,
            delete=lambda k: self.secrets.pop(k, None),
            _fernet=object(),
        )
        self.connector = DiscordConnector({"admin_users": ["101"]}, vault=self.vault)
        self.manager = SimpleNamespace(
            active_turns=0, _resolve_home_channel=AsyncMock(return_value=("discord", "601", "one"))
        )
        self.connector._agent_manager = self.manager
        self.connector.set_agent_configs({"worker": {"routing": {"discord": {"account": "one", "channels": ["601"]}}}})
        self.alerts = DiscordAlerts(
            self.connector, {"adapters": {"posthog": "extras.posthog.alerts:PostHogAdapter"}}, directory
        )
        self.connector._alerts = self.alerts
        self.bot = DiscordBot("one", self.connector, admin_users=["101"])
        self.user = SimpleNamespace(id=999)
        self.guild = SimpleNamespace(id=301, fetch_member=AsyncMock(return_value=self.user))
        self.room = SimpleNamespace(id=401, guild=self.guild, flags=0, name="sample")
        self.home_messages = []

        async def history(**kwargs):
            for message in reversed(self.home_messages):
                yield message

        async def send(text, **kwargs):
            assert kwargs["allowed_mentions"].everyone is False
            message = SimpleNamespace(id=800 + len(self.home_messages), author=self.user, webhook_id=None, content=text)
            self.home_messages.append(message)
            return message

        self.home = SimpleNamespace(id=601, history=history, send=AsyncMock(side_effect=send))
        self.channel_error = None

        async def fetch_channel(channel_id):
            if channel_id == self.home.id:
                return self.home
            if self.channel_error:
                raise self.channel_error
            return self.room

        self.bot.client = SimpleNamespace(
            user=self.user,
            fetch_guild=AsyncMock(return_value=self.guild),
            fetch_channel=AsyncMock(side_effect=fetch_channel),
        )
        self.connector.bots["one"] = self.bot
        self.store, self.lifecycle = self.alerts.store, self.alerts.lifecycle
        self.lifecycle.accounts.add("one")
        self.remote, self.calls = {}, []
        self.adapter = self.alerts.adapters["posthog"]
        self.adapter._request = self.request

    def source(self, state="active", channel_id="401", destination=True):
        source = self.store.begin("worker", "101", "one", str(uuid.uuid4()))
        config = {
            "service": "posthog",
            "host": "https://eu.posthog.com",
            "project": "123",
            "app": "sample",
            "api_key": "secrets/service-key",
            "triggers": ["created"],
        }
        source = self.store.update(source["id"], config=config, state=state, guild_id="301", channel_id=channel_id)
        self.secrets[f"secrets/alert-webhook-{source['id']}-r1"] = (
            "https://discord.com/api/webhooks/501/synthetic-token"
        )
        if destination:
            self.store.intent(source, "destination")
            destination_id = self.add_remote(source)
            self.store.finish_operation(source, "destination", {"id": destination_id})
            source = self.store.update(source["id"], config={**config, "destination_id": destination_id})
        return source

    def add_remote(self, source):
        destination_id = str(uuid.uuid4())
        webhook = self.secrets[f"secrets/alert-webhook-{source['id']}-r{source['revision']}"]
        self.remote[destination_id] = {**self.adapter._destination_payload(source, webhook), "id": destination_id}
        # Full retrieve/PATCH responses expose nested template, not template_id.
        template_id = self.remote[destination_id].pop("template_id")
        self.remote[destination_id]["template"] = {"id": template_id}
        return destination_id

    async def request(self, config, method, resource, **kwargs):
        self.calls.append((method, resource))
        if resource.startswith("hog_functions/?"):
            # Genuine minimal cards: no template_id or inputs.
            return {
                "results": [
                    {k: v for k, v in item.items() if k in {"id", "name", "filters", "enabled"}}
                    for item in self.remote.values()
                ],
                "next": None,
            }
        destination_id = resource.split("/")[1]
        if method == "GET":
            if destination_id not in self.remote:
                raise DestinationNotFoundError("PostHog destination GET returned HTTP 404")
            return copy.deepcopy(self.remote[destination_id])
        if method == "PATCH":
            assert kwargs["payload"] in ({"enabled": False}, {"enabled": False, "deleted": True})
            self.remote[destination_id]["enabled"] = False
            result = copy.deepcopy(self.remote[destination_id])
            if kwargs["payload"].get("deleted"):
                self.remote.pop(destination_id)
            # `deleted` is write_only, including the successful PATCH response.
            return result
        raise AssertionError("Cleanup must never POST an invocation/create or use HTTP DELETE")

    def gone(self):
        self.channel_error = http_error(404, 10003)

    def close(self):
        self.store.close()


def http_error(status, code):
    cls = {403: discord.Forbidden, 404: discord.NotFound}.get(status, discord.HTTPException)
    return cls(SimpleNamespace(status=status, reason="synthetic"), {"code": code, "message": "synthetic"})


@pytest.fixture
def harness(tmp_path):
    value = Harness(tmp_path)
    yield value
    value.close()


def enqueue(store, source, message_id):
    store.receive(source, event_id=str(uuid.uuid4()), issue_id=str(uuid.uuid4()), kind="created", message_id=message_id)


@pytest.mark.parametrize("state", ["draft", "provisioning", "provisional", "active", "paused", "disabled"])
async def test_confirmed_deletion_purges_all_states_after_owned_removal(harness, state):
    h = harness
    source = h.source("active", destination=state != "draft")
    enqueue(h.store, source, "701")
    running = h.store.claim()
    enqueue(h.store, source, "702")
    second = h.store.claim()
    h.store.save_result(second, "Unsent result")
    enqueue(h.store, source, "703")
    h.store.update(source["id"], state=state)
    h.gone()
    await h.bot.on_guild_channel_delete(h.room)
    assert h.store.get(source["id"])["state"] == "deleting"
    assert h.store.counts(source["id"]) == {"cancelled": 3}
    assert not h.store.save_result(running, "Late result")
    assert not h.store.ready()
    assert not h.calls  # Local revocation precedes vendor I/O.
    await h.lifecycle.cleanup_due()
    assert h.store.get(source["id"]) is None and h.store.channel("401") is None
    assert not h.store.db.execute("SELECT 1 FROM operations").fetchone()
    assert not h.remote
    assert h.secrets == {"secrets/service-key": "synthetic-key-never-used-on-network"}
    await h.lifecycle.deliver_notices()
    assert len(h.home_messages) == 1 and "removed after channel deletion" in h.home_messages[0].content
    assert h.manager._resolve_home_channel.await_args.args == ("worker",)
    assert h.home.send.await_count == 1


@pytest.mark.parametrize(
    "failure", [http_error(403, 50001), http_error(404, 10004), http_error(500, 0), TimeoutError()]
)
async def test_invisible_or_ambiguous_channel_is_preserved_and_reported_once(harness, failure):
    h = harness
    source = h.source()
    h.channel_error = failure
    # An empty listing cannot establish absence and is never consulted.
    h.guild.fetch_channels = AsyncMock(return_value=[])
    for _ in range(2):
        await h.lifecycle.reconcile("one")
        await h.lifecycle.cleanup_due()
        await h.lifecycle.deliver_notices()
    assert h.store.get(source["id"])["state"] == "active" and not h.calls
    h.guild.fetch_channels.assert_not_awaited()
    assert len(h.home_messages) == 1


async def test_obfuscated_channel_uses_id_and_preserves_registration(harness):
    h = harness
    source = h.source()
    h.room.flags = SimpleNamespace(value=1 << 17)
    h.room.name = "unreliable metadata"
    h.room.topic = "not an ownership marker"
    h.gone()  # Even a conflicting REST result cannot override an obfuscated event.
    await h.bot.on_guild_channel_update(SimpleNamespace(), h.room)
    assert h.store.get(source["id"])["state"] == "active"
    assert h.bot.client.fetch_channel.await_count == 0
    assert not h.calls
    await h.lifecycle.deliver_notices()
    assert "obfuscated" in h.home_messages[0].content


@pytest.mark.parametrize("loss", [http_error(403, 50001), http_error(404, 10004), TimeoutError()])
async def test_guild_loss_preserves_all_sources_and_reports_once(harness, loss):
    h = harness
    sources = [h.source(channel_id=str(401 + i)) for i in range(3)]
    h.gone()
    h.bot.client.fetch_guild.side_effect = loss
    await h.lifecycle.reconcile("one")
    await h.bot.on_guild_remove(h.guild)
    await h.lifecycle.deliver_notices()
    assert all(h.store.get(s["id"])["state"] == "active" for s in sources)
    assert h.bot.client.fetch_channel.await_args_list == [((601,),)]
    assert not h.calls and len(h.home_messages) == 1


async def test_guild_loss_between_specific_404_and_confirmation_is_preserved(harness):
    h = harness
    source = h.source()
    h.gone()
    h.bot.client.fetch_guild.side_effect = [h.guild, http_error(403, 50001)]
    await h.lifecycle.reconcile("one")
    assert h.store.get(source["id"])["state"] == "active" and not h.calls


async def test_deletion_while_stopped_is_reconciled_before_worker_start(tmp_path):
    first = Harness(tmp_path)
    source = first.source()
    remote, secrets = first.remote, first.secrets
    first.close()
    h = Harness(tmp_path)
    h.remote.update(remote)
    h.secrets.update(secrets)
    h.gone()
    h.alerts.credentials.start = AsyncMock()
    h.alerts.credentials.stop = AsyncMock()
    worker_started = asyncio.Event()

    async def worker():
        assert h.store.get(source["id"]) is None or h.store.get(source["id"])["state"] == "deleting"
        assert "one" in h.alerts.worker.accounts
        worker_started.set()
        await asyncio.Event().wait()

    h.alerts.worker.run = worker
    try:
        await h.alerts.start("one")
        await asyncio.wait_for(worker_started.wait(), 2)
        # No gateway channel-delete event was emitted.
        await h.lifecycle.cleanup_due()
        assert h.store.get(source["id"]) is None
        assert all(not r["enabled"] for r in h.remote.values())
    finally:
        await h.alerts.stop()


async def test_unsubscribe_retains_room_then_delete_purges(harness):
    h = harness
    source = h.source()
    h.lifecycle.request(source["id"], "unsubscribe")
    await h.lifecycle.cleanup_due()
    assert h.store.channel("401")["state"] == "disabled"
    assert len(h.remote) == 1 and not h.remote[source["config"]["destination_id"]]["enabled"]
    revision = h.store.db.execute("SELECT cleaned,removed FROM alert_revisions").fetchone()
    assert tuple(revision) == (1, 0)
    h.gone()
    await h.lifecycle.reconcile("one")
    await h.lifecycle.cleanup_due()
    assert h.store.channel("401") is None and not h.remote


async def test_retry_survives_reopen_and_exhaustion_retains_ownership(tmp_path):
    h = Harness(tmp_path)
    source = h.source()
    h.lifecycle.request(source["id"], "deleted")
    h.adapter.remove = AsyncMock(side_effect=RuntimeError("must not expose secret"))
    await h.lifecycle.cleanup_due(now=100000000000)
    job = dict(h.store.db.execute("SELECT * FROM teardowns").fetchone())
    assert job["attempts"] == 1 and job["state"] == "pending"
    await h.lifecycle.deliver_notices()
    assert "will retry" in h.home_messages[0].content
    remote, secrets = h.remote, h.secrets
    h.close()
    h = Harness(tmp_path)
    h.remote.update(remote)
    h.secrets.update(secrets)
    h.adapter.remove = AsyncMock(side_effect=RuntimeError("must not expose secret"))
    try:
        await h.lifecycle.cleanup_due(now=job["available"] - 1)
        h.adapter.remove.assert_not_awaited()
        for _ in range(7):
            job = dict(h.store.db.execute("SELECT * FROM teardowns").fetchone())
            await h.lifecycle.cleanup_due(now=job["available"])
        job = dict(h.store.db.execute("SELECT * FROM teardowns").fetchone())
        assert job["attempts"] == 8 and job["state"] == "held"
        assert h.store.get(source["id"])["state"] == "deleting"
        assert "must not expose secret" not in "\n".join(h.store.db.iterdump())
        await h.lifecycle.deliver_notices()
        assert "eight failed attempts" in h.home_messages[0].content
        h.adapter.remove = PostHogAdapter.remove.__get__(h.adapter)
        h.store.retry_teardown(source["id"])
        await h.lifecycle.cleanup_due()
        assert h.store.get(source["id"]) is None
    finally:
        h.close()


async def test_reconcile_ambiguous_create_then_remove_never_creates(harness):
    h = harness
    source = h.source("provisional", destination=False)
    h.store.intent(source, "destination")
    destination_id = h.add_remote(source)  # Create succeeded; its response was lost.
    h.lifecycle.request(source["id"], "deleted")
    await h.lifecycle.cleanup_due()
    assert h.store.get(source["id"]) is None
    assert destination_id not in h.remote
    assert [method for method, _ in h.calls] == ["GET", "GET", "GET", "PATCH", "GET", "GET"]


async def test_unknown_create_without_remote_match_retains_recovery_record(harness):
    h = harness
    source = h.source("provisional", destination=False)
    h.store.intent(source, "destination")
    h.lifecycle.request(source["id"], "deleted")
    await h.lifecycle.cleanup_due()
    assert h.store.get(source["id"])["state"] == "deleting"
    assert h.store.db.execute("SELECT state FROM operations").fetchone()[0] == "intent"
    assert len(h.secrets) == 2
    assert all(method == "GET" for method, _ in h.calls)


async def test_changed_owned_destination_is_never_removed_or_forgotten(harness):
    h = harness
    source = h.source()
    h.remote[source["config"]["destination_id"]]["inputs"]["content"]["value"] = "changed remotely"
    h.lifecycle.request(source["id"], "deleted")
    await h.lifecycle.cleanup_due()
    assert h.store.get(source["id"])["state"] == "deleting"
    assert all(method == "GET" for method, _ in h.calls)


async def test_channel_deleted_during_destination_create_waits_for_journal(harness):
    h = harness
    source = h.source("provisional", destination=False)
    webhook = h.secrets[f"secrets/alert-webhook-{source['id']}-r1"]
    h.alerts.transport.provision = AsyncMock(return_value=(source, webhook))
    started, finish = asyncio.Event(), asyncio.Event()
    original_request = h.adapter._request

    async def request(config, method, resource, **kwargs):
        if method == "POST":
            assert resource == "hog_functions/"
            started.set()
            await finish.wait()
            destination_id = h.add_remote(source)
            return copy.deepcopy(h.remote[destination_id])
        return await original_request(config, method, resource, **kwargs)

    h.adapter._request = request
    h.adapter.test_delivery = AsyncMock(side_effect=AssertionError("must not invoke after deletion"))

    async def provision():
        async with h.alerts.locks.setdefault(source["id"], asyncio.Lock()):
            return await h.alerts.provision(source)

    task = asyncio.create_task(provision())
    await asyncio.wait_for(started.wait(), 2)
    h.gone()
    await h.bot.on_guild_channel_delete(h.room)
    assert h.store.get(source["id"])["state"] == "deleting"
    await h.lifecycle.cleanup_due()
    assert h.store.get(source["id"]) is not None and not h.remote
    finish.set()
    with pytest.raises(AlertError, match="revoked"):
        await task
    assert h.store.db.execute("SELECT state FROM operations WHERE step='destination'").fetchone()[0] == "complete"
    await h.lifecycle.cleanup_due()
    assert h.store.get(source["id"]) is None
    assert not h.remote
    h.adapter.test_delivery.assert_not_awaited()


async def test_inflight_diagnosis_cancelled_without_stopping_shared_worker(harness):
    h = harness
    source = h.source()
    enqueue(h.store, source, "701")
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def issue(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    h.adapter.issue = issue
    h.alerts.worker.accounts = {"one"}
    h.alerts.worker.transport.progress = AsyncMock()
    task = asyncio.create_task(h.alerts.worker.once())
    await asyncio.wait_for(started.wait(), 2)
    h.lifecycle.request(source["id"], "deleted")
    assert await asyncio.wait_for(task, 2) is True
    assert cancelled.is_set() and h.manager.active_turns == 0
    assert h.store.counts(source["id"]) == {"cancelled": 1}
    await h.lifecycle.cleanup_due()
    assert h.store.get(source["id"]) is None
    assert await h.alerts.worker.once() is False


async def test_all_rotation_revisions_removed_using_original_nonce(harness):
    h = harness
    first = h.source()
    second = h.store.rotate(first["id"])
    assert "destination_id" not in second["config"]
    h.secrets[f"secrets/alert-webhook-{second['id']}-r2"] = "https://discord.com/api/webhooks/502/other-synthetic-token"
    h.store.intent(second, "destination")
    second_id = h.add_remote(second)
    h.store.finish_operation(second, "destination", {"id": second_id})
    h.lifecycle.request(first["id"], "deleted")
    await h.lifecycle.cleanup_due()
    assert h.store.get(first["id"]) is None
    assert not h.remote


async def test_home_notice_survives_source_purge_and_failed_send(harness):
    h = harness
    source = h.source()
    h.lifecycle.request(source["id"], "deleted")
    await h.lifecycle.cleanup_due()
    original_send = h.home.send
    h.home.send = AsyncMock(side_effect=http_error(403, 50001))
    await h.lifecycle.deliver_notices()
    assert h.store.get(source["id"]) is None
    notice = h.store.db.execute("SELECT * FROM lifecycle_notices").fetchone()
    assert notice["state"] == "pending"
    h.home.send = original_send
    await h.lifecycle.deliver_notices(now=notice["available"])
    assert len(h.home_messages) == 1
    await h.lifecycle.deliver_notices(now=notice["available"] + 100)
    assert len(h.home_messages) == 1


async def test_lost_notice_send_response_is_reconciled_without_duplicate(harness):
    h = harness
    source = h.source()
    h.lifecycle.request(source["id"], "deleted")
    await h.lifecycle.cleanup_due()
    send = h.home.send

    async def lost(*args, **kwargs):
        await send(*args, **kwargs)
        raise TimeoutError()

    h.home.send = lost
    await h.lifecycle.deliver_notices()
    notice = h.store.db.execute("SELECT * FROM lifecycle_notices").fetchone()
    assert notice["state"] == "sending" and len(h.home_messages) == 1
    await h.lifecycle.deliver_notices(now=notice["available"])
    assert h.store.db.execute("SELECT state FROM lifecycle_notices").fetchone()[0] == "complete"
    assert len(h.home_messages) == 1


async def test_cleanup_does_not_enable_an_unreconciled_account(harness):
    h = harness
    source = h.source()
    enqueue(h.store, source, "701")
    assert await h.alerts.worker.once() is False
    assert h.store.counts(source["id"]) == {"pending": 1}


async def test_remove_patch_echo_is_not_cleanup_confirmation(harness):
    h = harness
    source = h.source()
    original = h.adapter._request

    async def unsaved(config, method, resource, **kwargs):
        if method == "PATCH":
            h.calls.append((method, resource))
            return {**copy.deepcopy(h.remote[source["config"]["destination_id"]]), "enabled": False}
        return await original(config, method, resource, **kwargs)

    h.adapter._request = unsaved
    h.lifecycle.request(source["id"], "deleted")
    await h.lifecycle.cleanup_due()
    assert h.store.get(source["id"])["state"] == "deleting"
    assert h.store.db.execute("SELECT attempts FROM teardowns").fetchone()[0] == 1
    assert h.calls[-1][0] == "GET"


async def test_deletion_upgrades_inflight_unsubscribe_without_leaving_completed_tombstone(harness):
    h = harness
    source = h.source()
    started, finish = asyncio.Event(), asyncio.Event()
    disable = h.adapter.disable

    async def delayed(*args):
        started.set()
        await finish.wait()
        await disable(*args)

    h.adapter.disable = delayed
    h.lifecycle.request(source["id"], "unsubscribe")
    task = asyncio.create_task(h.lifecycle.cleanup_due())
    await asyncio.wait_for(started.wait(), 2)
    h.lifecycle.request(source["id"], "deleted")
    finish.set()
    await task
    assert h.store.get(source["id"]) is None
    assert not h.remote
    assert [method for method, _ in h.calls].count("PATCH") == 2


async def test_cancel_unsent_result_during_channel_lookup(harness):
    h = harness
    source = h.source()
    enqueue(h.store, source, "701")
    receipt = h.store.claim()
    h.store.save_result(receipt, "result must never be delivered")
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def report(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    h.alerts.worker.accounts = {"one"}
    h.alerts.transport.report = report
    task = asyncio.create_task(h.alerts.worker.once())
    await asyncio.wait_for(started.wait(), 2)
    h.lifecycle.request(source["id"], "deleted")
    assert await asyncio.wait_for(task, 2) is False
    assert cancelled.is_set()
    assert h.store.counts(source["id"]) == {"cancelled": 1}
    assert h.store.db.execute("SELECT result FROM receipts").fetchone()[0] is None


async def test_completed_channel_creation_recovered_after_crash_before_source_update(harness):
    h = harness
    source = h.source("provisioning", channel_id=None, destination=False)
    h.store.intent(source, "channel")
    h.store.finish_operation(source, "channel", {"id": "401"})
    h.gone()
    await h.lifecycle.reconcile("one")
    assert h.store.get(source["id"])["state"] == "deleting"
    await h.lifecycle.cleanup_due()
    assert h.store.get(source["id"]) is None and not h.calls


async def test_deletion_event_locates_inflight_channel_but_still_requires_http_absence(harness):
    h = harness
    source = h.source("provisioning", channel_id=None, destination=False)
    h.store.intent(source, "channel")
    h.room.topic = "kbots-alert:" + source["id"]
    h.channel_error = http_error(403, 50001)
    await h.bot.on_guild_channel_delete(h.room)
    assert h.store.get(source["id"])["channel_id"] is None
    h.gone()
    await h.bot.on_guild_channel_delete(h.room)
    assert h.store.get(source["id"])["state"] == "deleting"
    await h.lifecycle.cleanup_due()
    assert h.store.get(source["id"]) is None


async def test_unknown_channel_create_is_held_and_reported_without_invented_absence(harness):
    h = harness
    source = h.source("provisioning", channel_id=None, destination=False)
    h.store.intent(source, "channel")
    h.gone()
    await h.lifecycle.reconcile("one")
    await h.lifecycle.deliver_notices()
    assert h.store.get(source["id"])["state"] == "provisioning"
    assert "uncertain channel creation" in h.home_messages[0].content
    assert not h.calls


async def test_missing_or_wrong_home_keeps_notice_durable_without_using_deleted_room(harness):
    h = harness
    source = h.source()
    h.lifecycle.request(source["id"], "deleted")
    await h.lifecycle.cleanup_due()
    for home in (None, ("discord", "401", "one"), ("discord", "601", "another-account")):
        h.manager._resolve_home_channel.return_value = home
        await h.lifecycle.deliver_notices(now=100000000000)
    assert h.store.db.execute("SELECT state FROM lifecycle_notices").fetchone()[0] == "pending"
    assert not h.home_messages


async def test_unknown_notice_dispatch_is_not_resent_without_positive_receipt(harness):
    h = harness
    source = h.source()
    h.lifecycle.request(source["id"], "deleted")
    await h.lifecycle.cleanup_due()
    h.home.send = AsyncMock(side_effect=TimeoutError())
    await h.lifecycle.deliver_notices()
    notice = h.store.db.execute("SELECT * FROM lifecycle_notices").fetchone()
    await h.lifecycle.deliver_notices(now=notice["available"])
    assert h.home.send.await_count == 1
    assert h.store.db.execute("SELECT state FROM lifecycle_notices").fetchone()[0] == "sending"


@pytest.mark.parametrize("invalid_id", [None, "----", 12])
async def test_malformed_destination_card_yields_safe_cleanup_error(harness, invalid_id):
    h = harness
    source = h.source("provisional", destination=False)
    h.store.intent(source, "destination")
    name = f"kbots-alert-{source['id']}-r1"
    h.adapter._request = AsyncMock(return_value={"results": [{"name": name, "id": invalid_id}], "next": None})
    with pytest.raises(AlertError, match="identifier is invalid"):
        await h.adapter.reconcile_destination(source)
    assert h.adapter._request.await_count == 1


async def test_obfuscation_survives_actual_discordpy_gateway_parser(harness):
    from src.connectors.discord_alerts import AlertChannelClient

    h = harness
    source = h.source()
    client = AlertChannelClient(intents=discord.Intents.none())
    state = client._connection
    state.dispatch = Mock()
    state._add_ready_state = lambda _: False
    state._guild_needs_chunking = lambda _: False
    channel_payload = {
        "id": "401",
        "guild_id": "301",
        "type": 0,
        "name": "obfuscated",
        "position": 0,
        "flags": 1 << 17,
        "permission_overwrites": [],
    }
    guild_payload = {
        "id": "301",
        "name": "test guild",
        "roles": [],
        "channels": [channel_payload],
        "members": [],
        "emojis": [],
        "features": [],
    }
    state.parsers["GUILD_CREATE"](guild_payload)
    assert state.alert_obfuscated == {"401": "301"}
    channel = client.get_channel(401)
    assert isinstance(channel, discord.TextChannel)
    # discord.py 2.7 drops the flag; the narrow shim must still preserve it.
    state.user = h.user
    client.fetch_guild = AsyncMock(return_value=h.guild)
    client.fetch_channel = AsyncMock(side_effect=http_error(404, 10003))
    h.bot.client = client
    await h.lifecycle.reconcile("one")
    assert h.store.get(source["id"])["state"] == "active"
    client.fetch_channel.assert_not_awaited()
    state.parsers["CHANNEL_UPDATE"]({**channel_payload, "flags": 0, "name": "actual name"})
    assert not state.alert_obfuscated
    state.parsers["CHANNEL_CREATE"]({**channel_payload, "id": "402"})
    assert state.alert_obfuscated == {"402": "301"}
    state.parsers["CHANNEL_DELETE"]({"id": "402", "guild_id": "301", "type": 0})
    assert not state.alert_obfuscated
    state.parsers["CHANNEL_UPDATE"](channel_payload)
    state.clear()
    assert not state.alert_obfuscated
    await client.close()


async def test_cached_obfuscated_channel_is_not_deleted_on_startup_without_event(harness):
    h = harness
    source = h.source()
    h.bot.client.get_channel = lambda _: SimpleNamespace(flags=1 << 17)
    h.gone()
    await h.lifecycle.reconcile("one")
    assert h.store.get(source["id"])["state"] == "active"
    h.bot.client.fetch_channel.assert_not_awaited()


async def test_removal_response_lost_reconciles_after_restart_without_second_patch(tmp_path):
    h = Harness(tmp_path)
    source = h.source()
    original = h.adapter._request

    async def lost_response(config, method, resource, **kwargs):
        result = await original(config, method, resource, **kwargs)
        if method == "PATCH":
            raise AlertError("PostHog request failed or its outcome is unknown")
        return result

    h.adapter._request = lost_response
    h.lifecycle.request(source["id"], "deleted")
    await h.lifecycle.cleanup_due()
    assert not h.remote and h.store.get(source["id"]) is not None
    assert h.store.db.execute("SELECT removed FROM alert_revisions").fetchone()[0] == 0
    job = dict(h.store.db.execute("SELECT * FROM teardowns").fetchone())
    secrets = dict(h.secrets)
    h.close()
    h = Harness(tmp_path)
    h.secrets.update(secrets)
    try:
        await h.lifecycle.cleanup_due(now=job["available"])
        assert h.store.get(source["id"]) is None
        assert [method for method, _ in h.calls] == ["GET", "GET"]
        assert h.secrets == {"secrets/service-key": "synthetic-key-never-used-on-network"}
    finally:
        h.close()


async def test_previous_schema_disabled_revision_must_be_removed_before_purge(tmp_path):
    h = Harness(tmp_path)
    source = h.source()
    h.lifecycle.request(source["id"], "unsubscribe")
    await h.lifecycle.cleanup_due()
    assert h.remote[source["config"]["destination_id"]]["enabled"] is False
    # An actual pre-v9 schema with a completed disable, not a synthetic new row.
    h.store.db.execute("ALTER TABLE alert_revisions DROP COLUMN removed")
    remote, secrets = copy.deepcopy(h.remote), dict(h.secrets)
    h.close()
    h = Harness(tmp_path)
    h.remote.update(remote)
    h.secrets.update(secrets)
    try:
        assert tuple(h.store.db.execute("SELECT cleaned,removed FROM alert_revisions").fetchone()) == (1, 0)
        h.lifecycle.request(source["id"], "deleted")
        with pytest.raises(AlertError, match="still pending"):
            h.store.purge_deleted(source["id"])
        await h.lifecycle.cleanup_due()
        assert h.store.get(source["id"]) is None and not h.remote
        assert [method for method, _ in h.calls] == ["GET", "PATCH", "GET", "GET"]
    finally:
        h.close()


async def test_missing_list_confirmation_retains_credentials_and_recovery_record(harness):
    h = harness
    source = h.source()
    original = h.adapter._request

    async def forbidden_list(config, method, resource, **kwargs):
        if resource.startswith("hog_functions/?"):
            raise AlertError("PostHog returned HTTP 403")
        return await original(config, method, resource, **kwargs)

    h.adapter._request = forbidden_list
    h.lifecycle.request(source["id"], "deleted")
    await h.lifecycle.cleanup_due()
    assert not h.remote
    assert h.store.get(source["id"])["state"] == "deleting"
    assert h.store.db.execute("SELECT removed FROM alert_revisions").fetchone()[0] == 0
    assert h.secrets[f"secrets/alert-webhook-{source['id']}-r1"]
    h.adapter._request = original
    h.store.retry_teardown(source["id"])
    await h.lifecycle.cleanup_due()
    assert h.store.get(source["id"]) is None
    assert [method for method, _ in h.calls].count("PATCH") == 1


async def test_completed_removal_of_old_revision_survives_retry_of_new_revision(harness):
    h = harness
    first = h.source()
    first_id = first["config"]["destination_id"]
    second = h.store.rotate(first["id"])
    h.secrets[f"secrets/alert-webhook-{first['id']}-r2"] = "https://discord.com/api/webhooks/502/second-test-token"
    h.store.intent(second, "destination")
    second_id = h.add_remote(second)
    h.store.finish_operation(second, "destination", {"id": second_id})
    original = h.adapter._request

    async def unavailable(config, method, resource, **kwargs):
        if method == "PATCH" and second_id in resource:
            raise AlertError("PostHog returned HTTP 503")
        return await original(config, method, resource, **kwargs)

    h.adapter._request = unavailable
    h.lifecycle.request(first["id"], "deleted")
    await h.lifecycle.cleanup_due()
    assert first_id not in h.remote and second_id in h.remote
    assert [tuple(row) for row in h.store.db.execute(
        "SELECT revision,removed FROM alert_revisions ORDER BY revision"
    )] == [(1, 1), (2, 0)]
    assert len(h.secrets) == 3  # Retain recovery references until terminal cleanup.
    h.calls.clear()
    h.adapter._request = original
    h.store.retry_teardown(first["id"])
    await h.lifecycle.cleanup_due()
    assert h.store.get(first["id"]) is None and not h.remote
    assert all(first_id not in resource for _, resource in h.calls)
    assert [method for method, _ in h.calls] == ["GET", "PATCH", "GET", "GET"]
