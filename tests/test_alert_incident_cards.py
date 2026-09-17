"""Durable intake cleanup, webhook ownership and single-card incident delivery."""

import asyncio
import copy
import json
import sqlite3
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from src.connectors.alert_embeds import AMBER, GREY, RED, incident_embed, status_nonce
from src.core.alert_channels import AlertError, AlertStore, UncertainOperationError
from tests.test_alert_evidence_wait import delayed as delayed_fixture
from tests.test_alert_lifecycle import Harness, http_error
from tests.test_alert_setup_ux import visible_text
from tests.test_discord_alert_channels import message

delayed = delayed_fixture


@pytest.fixture
def owned(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    source = h.source()
    source = h.store.update(source["id"], webhook_id="501")
    h.store.intent(source, "webhook")
    h.store.finish_operation(source, "webhook", {"id": "501"})
    hook = SimpleNamespace(delete_message=AsyncMock(), edit=AsyncMock())
    factory = monkeypatch.setattr  # No real HTTP client or credential is used.
    factory(discord.Webhook, "from_url", lambda url, **kwargs: hook)
    h.hook, h.source_row = hook, source
    yield h
    h.close()


async def test_webhook_delete_observes_committed_receipt_and_deduplicates(owned):
    h = owned
    msg = message(h.source_row)
    event_id = msg.content.split()[4]

    async def delete(mid):
        assert mid == msg.id and not h.store.db.in_transaction
        with sqlite3.connect(h.store.path) as independent:
            assert independent.execute("SELECT event_id FROM receipts").fetchone()[0] == event_id

    h.hook.delete_message.side_effect = delete
    await h.alerts.on_message(h.bot, msg)
    duplicate = copy.copy(msg)
    duplicate.id += 1
    h.hook.delete_message.side_effect = None
    await h.alerts.on_message(h.bot, duplicate)
    assert h.hook.delete_message.await_args_list[0].args == (msg.id,)
    assert h.hook.delete_message.await_args_list[1].args == (duplicate.id,)
    assert h.store.counts(h.source_row["id"]) == {"pending": 1}


@pytest.mark.parametrize("wrong", ["nonce", "webhook", "guild", "author", "body", "token", "journal", "issue"])
async def test_unowned_or_unparsed_messages_are_never_deleted(owned, wrong):
    h = owned
    msg = message(h.source_row)
    if wrong == "nonce":
        msg.content = msg.content.replace(h.source_row["nonce"], "wrong")
    elif wrong == "webhook":
        msg.webhook_id = 502
    elif wrong == "guild":
        msg.guild.id = 302
    elif wrong == "author":
        msg.author.bot = False
    elif wrong == "body":
        msg.content = "Please delete this arbitrary message"
    elif wrong == "token":
        h.secrets[f"secrets/alert-webhook-{h.source_row['id']}-r1"] = "https://evil.example/webhooks/501/secret"
    elif wrong == "journal":
        h.store.db.execute("DELETE FROM operations WHERE step='webhook'")
    elif wrong == "issue":
        event = h.adapter.parse_event(h.source_row, msg.content)
        h.store.receive(h.source_row, message_id=900, **{**event, "issue_id": str(uuid.uuid4())})
    await h.alerts.on_message(h.bot, msg)
    h.hook.delete_message.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["overflow", "inactive"])
async def test_not_accepted_is_never_deleted(owned, monkeypatch, outcome):
    h = owned
    monkeypatch.setattr(h.store, "receive", lambda *a, **k: outcome)
    h.alerts.transport.say = AsyncMock()
    await h.alerts.on_message(h.bot, message(h.source_row))
    h.hook.delete_message.assert_not_awaited()


@pytest.mark.parametrize("error", [TimeoutError("secret-sentinel"), http_error(403, 50001)])
async def test_failed_delete_keeps_durable_work_without_amplifying(owned, caplog, error):
    h = owned
    h.hook.delete_message.side_effect = error
    await h.alerts.on_message(h.bot, message(h.source_row))
    assert h.store.counts(h.source_row["id"]) == {"pending": 1}
    assert h.alerts.worker.wake.is_set()
    assert "accepted webhook message deletion" in caplog.text
    assert "secret-sentinel" not in caplog.text


async def test_cancelled_delete_leaves_durable_receipt(owned):
    h = owned
    h.hook.delete_message.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await h.alerts.on_message(h.bot, message(h.source_row))
    assert h.store.counts(h.source_row["id"]) == {"pending": 1}


async def test_sdk_delete_uses_webhook_token_endpoint_only():
    from discord.webhook.async_ import async_context

    adapter = SimpleNamespace(delete_webhook_message=AsyncMock())
    handle = async_context.set(adapter)
    try:
        hook = discord.Webhook.partial(501, token="synthetic-token", session=SimpleNamespace())
        await hook.delete_message(701)
        call = adapter.delete_webhook_message.await_args
        assert call.args == (501, "synthetic-token", 701)
        assert "auth_token" not in call.kwargs
    finally:
        async_context.reset(handle)


def remote_hook(h):
    return SimpleNamespace(id=501, channel_id=401, guild_id=301, user=h.user, name=f"kbots-{h.source_row['id']}-r1")


async def test_owned_legacy_webhook_renamed_after_fresh_identity_read(owned):
    h = owned
    h.bot.client.fetch_webhook = AsyncMock(return_value=remote_hook(h))
    h.hook.edit.return_value = SimpleNamespace(id=501, name="PostHog alerts")
    before = h.adapter._destination_payload(h.source_row, h.secrets[f"secrets/alert-webhook-{h.source_row['id']}-r1"])
    await h.lifecycle.reconcile("one")
    h.hook.edit.assert_awaited_once_with(name="PostHog alerts", prefer_auth=False)
    await h.lifecycle.reconcile("one")
    assert h.hook.edit.await_count == 1
    assert (
        h.adapter._destination_payload(h.source_row, h.secrets[f"secrets/alert-webhook-{h.source_row['id']}-r1"])
        == before
    )
    assert not h.calls


@pytest.mark.parametrize(
    "field,value", [("id", 502), ("channel_id", 402), ("guild_id", 302), ("user", None), ("name", "Custom name")]
)
async def test_changed_webhook_is_not_renamed(owned, field, value):
    h = owned
    hook = remote_hook(h)
    setattr(hook, field, value)
    h.bot.client.fetch_webhook = AsyncMock(return_value=hook)
    await h.alerts.transport.refresh_webhook_name(h.source_row)
    h.hook.edit.assert_not_awaited()


async def test_rename_refuses_revocation_during_read(owned):
    h = owned

    async def revoked(_):
        h.store.disable(h.source_row["id"])
        return remote_hook(h)

    h.bot.client.fetch_webhook = revoked
    await h.alerts.transport.refresh_webhook_name(h.source_row)
    h.hook.edit.assert_not_awaited()


def receipt(**overrides):
    return {
        "id": str(uuid.uuid4()),
        "issue_id": str(uuid.uuid4()),
        "kind": "$error_tracking_issue_reopened",
        "issue_title": "sample: failure in handler",
        "success": True,
        "source_summary": "Source: `server/api/debug/boom.get.ts`.",
        "result": "**Verdict** Input needs validation. **Cause** Missing field. "
        "**Fix** Validate it. **Missing evidence** Reproduction.",
        **overrides,
    }


def source():
    return {"config": {"app": "sample", "service": "posthog", "host": "https://eu.posthog.com", "project": "123"}}


def test_real_card_has_one_linked_title_three_bounded_fields_and_human_footer():
    row = receipt()
    card = incident_embed(source(), row, "result")
    assert card.colour.value == RED
    assert card.title == row["issue_title"] and card.url.endswith(row["issue_id"])
    assert card.description == "Input needs validation."
    assert [(f.name, f.value) for f in card.fields] == [
        ("Cause", "Missing field."),
        ("Fix", "Validate it."),
        ("Missing evidence", "Reproduction."),
    ]
    assert card.footer.text == f"PostHog · reopened · issue {row['issue_id'][:8]}"
    assert "[alert:" not in json.dumps(card.to_dict())


def test_structured_result_retains_durable_missing_stack_trace_warning():
    row = receipt(evidence={"status": "timed_out"})
    card = incident_embed(source(), row, "result")
    assert card.fields[2].name == "Missing evidence"
    assert card.fields[2].value == "Stack trace was not yet available after a 90-second wait. Reproduction."


@pytest.mark.parametrize("kind", ["drill", "setup_test"])
def test_compact_success_is_grey_source_only_and_held_is_amber(kind):
    row = receipt(**{kind: True})
    card = incident_embed(source(), row, "result")
    assert card.colour.value == GREY and not card.fields
    assert len(card.description.splitlines()) == 2 and "boom.get.ts" in card.description
    assert "Missing field" not in card.description and "Deliberate drill" not in card.description
    held = incident_embed(source(), {**row, "success": False}, "result")
    assert held.colour.value == AMBER and "Alert path works" not in held.description


async def test_same_embed_progression_and_restart_keep_id_and_frame_source(delayed):
    d = delayed
    d.raw["results"][0]["uuid"] = d.h.store.pending()[0]["event_id"]
    pending = d.h.store.pending()[0]
    await d.h.alerts.transport.queued(d.source, pending)
    assert d.room.messages[0].embeds[0].description == "Queued for diagnosis."
    await d.worker.once()
    assert d.room.messages[0].embeds[0].description == "Waiting for stack trace."
    d.clock.now = 1030
    await d.worker.once()
    assert d.room.messages[0].embeds[0].description == "Investigating."
    d.h.store.close()
    d.h.store = AlertStore(d.h.directory)
    d.h.alerts.transport.store = d.h.store
    row = d.h.store.ready()[0]
    await d.h.alerts.transport.report(d.h.store.get(row["source_id"]), row)
    assert len(d.room.messages) == 1 and d.room.messages[0].content == ""
    card = d.room.messages[0].embeds[0]
    assert card.description.startswith("Drill received.") and "boom.get.ts" in card.description
    assert not card.fields and card.colour.value == GREY
    assert d.room.messages[0].nonce == status_nonce(row)


async def test_persisted_message_id_survives_nonce_disappearing(delayed):
    d = delayed
    row = d.h.store.pending()[0]
    await d.h.alerts.transport.queued(d.source, row)
    d.room.messages[0].nonce = None  # Nonce is optional on later Discord reads.
    await d.worker.once()
    assert len(d.room.messages) == 1
    assert "Waiting for stack trace" in d.room.messages[0].embeds[0].description


async def test_lost_send_without_nonce_refuses_duplicate(delayed):
    d = delayed
    row = d.h.store.pending()[0]
    original = d.room.send

    async def lost(*args, **kwargs):
        sent = await original(*args, **kwargs)
        sent.nonce = None
        raise TimeoutError()

    d.room.send = lost
    with pytest.raises(TimeoutError):
        await d.h.alerts.transport.queued(d.source, row)
    with pytest.raises(UncertainOperationError):
        await d.h.alerts.transport.queued(d.source, row)
    assert len(d.room.messages) == 1


@pytest.mark.parametrize("recovery", ["legacy", "saved_id", "human_name_only"])
async def test_webhook_creation_recovery_does_not_adopt_a_human_name(owned, recovery):
    h = owned
    source_row = h.store.update(h.source_row["id"], state="provisioning", webhook_id=None)
    h.store.db.execute("UPDATE operations SET state='intent',result=NULL WHERE step='webhook'")
    secret = f"secrets/alert-webhook-{source_row['id']}-r1"
    if recovery != "saved_id":
        h.secrets.pop(secret)
    hook = SimpleNamespace(
        id=501,
        name=f"kbots-{source_row['id']}-r1" if recovery == "legacy" else "PostHog alerts",
        user=h.user,
        token="synthetic",
        url="https://discord.com/api/webhooks/501/synthetic-token",
    )
    h.bot.client.get_guild = lambda _: h.guild
    h.room.topic = "kbots-alert:" + source_row["id"]
    h.room.webhooks = AsyncMock(return_value=[hook])
    h.room.create_webhook = AsyncMock(side_effect=AssertionError("Never repeat an ambiguous create"))
    if recovery == "human_name_only":
        with pytest.raises(UncertainOperationError):
            await h.alerts.transport.provision(source_row)
        assert secret not in h.secrets
    else:
        updated, url = await h.alerts.transport.provision(source_row)
        assert updated["webhook_id"] == "501" and updated["state"] == "provisional"
        assert url == hook.url
    h.room.create_webhook.assert_not_awaited()


def test_large_fields_are_bounded_and_redacted_without_leaking_protocol():
    row = receipt(
        result="**Verdict**\n"
        + "Sentence. " * 100
        + "\n**Cause**\n"
        + "🙂 Observation. " * 200
        + "\n**Fix**\n@everyone phx_"
        + "synthetic" * 10
        + "\n**Missing evidence**\nNo reproduction."
    )
    card = incident_embed(source(), row, "result")
    assert len(card.description.encode("utf-16-le")) // 2 <= 280
    assert len(card.fields[0].value.encode("utf-16-le")) // 2 <= 700
    assert card.fields[0].value.endswith("…")
    assert "@everyone" not in json.dumps(card.to_dict()) and "phx_" not in json.dumps(card.to_dict())
    assert len(card) < 6000


async def test_legacy_marker_send_recovers_then_converts_to_one_card(delayed):
    d = delayed
    row = d.h.store.pending()[0]
    d.h.store.intent(d.source, "status:" + row["id"])
    marker = f"[alert:{row['id']}:status]"
    await d.room.send(
        content="Legacy text",
        embed=discord.Embed().set_footer(text=marker),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await d.h.alerts.transport.queued(d.source, row)
    assert len(d.room.messages) == 1 and d.room.messages[0].content == ""
    assert marker not in visible_text(d.room.messages[0])
    assert d.room.messages[0].embeds[0].description == "Queued for diagnosis."


async def test_wrong_author_on_persisted_status_id_is_never_edited(delayed):
    d = delayed
    row = d.h.store.pending()[0]
    await d.h.alerts.transport.queued(d.source, row)
    d.room.messages[0].author.id = 1001
    with pytest.raises(AlertError):
        await d.h.alerts.transport.progress(d.source, row)
    assert len(d.room.messages) == 1 and not d.room.edits


async def test_duplicate_nonce_matches_refuse_ambiguous_recovery(delayed):
    d = delayed
    row = d.h.store.pending()[0]
    d.h.store.intent(d.source, "status:" + row["id"])
    for _ in range(2):
        await d.room.send(
            content="",
            embed=incident_embed(d.source, row, "queued", "Queued for diagnosis."),
            nonce=status_nonce(row),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    with pytest.raises(UncertainOperationError):
        await d.h.alerts.transport.queued(d.source, row)
    assert len(d.room.messages) == 2 and not d.room.edits
