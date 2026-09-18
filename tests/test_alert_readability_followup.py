"""Long reports, clone confirmations and recoverable Discord access failures."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.connectors.discord_alerts import DiscordAlertTransport, has_marker, marked_message
from src.core.alert_channels import AlertError
from src.core.alert_diagnosis import public_prose
from src.core.base import LLMResponse
from tests.test_alert_evidence_wait import delayed as delayed_fixture
from tests.test_alert_lifecycle import Harness, http_error
from tests.test_alert_repositories import REMOTE, repository
from tests.test_alert_setup_ux import MemoryChannel, visible_text
from tests.test_alert_setup_ux import ux as ux_fixture

delayed = delayed_fixture
ux = ux_fixture


@pytest.mark.parametrize(
    "text,limit,expected",
    [
        ("Short complete sentence.", 30, "Short complete sentence."),
        ("First line\nThe next line continues for a long time", 35, "First line…"),
        ("First sentence. The next sentence goes on for a long time", 35, "First sentence.…"),
        ("These words have no punctuation and keep going", 25, "These words have no…"),
        ("a" * 500, 80, "…"),
        ("🙂 🙂 🙂 🙂 🙂 🙂", 9, "🙂 🙂…"),
    ],
)
def test_prose_budget_has_readable_boundary_and_ellipsis(text, limit, expected):
    assert public_prose(text, limit) == expected
    assert len(public_prose(text, limit).encode("utf-16-le")) // 2 <= limit


@pytest.mark.parametrize("embed_links", [True, False])
async def test_wire_truncation_preserves_recovery_marker_without_midword_cut(embed_links):
    room = MemoryChannel(guild=SimpleNamespace(me=object()), embed_links=embed_links)
    marker = "[alert:00000000-0000-0000-0000-000000000000:status]"
    text = "[Example](https://example.com/issue)\n\n" + "This is a complete observation.\n" * 100
    formatted = marked_message(text, marker, room)
    content = formatted["content"]
    body = content if embed_links else content.removesuffix("\n||" + marker + "||")
    assert body.endswith("observation.…")
    assert len(content.encode("utf-16-le")) // 2 <= 2000
    message = SimpleNamespace(content=content, embeds=[formatted["embed"]] if embed_links else [])
    assert has_marker(message, marker)
    assert marked_message(content if embed_links else body, marker, room)["content"] == content


async def test_long_model_result_is_trimmed_before_storage_and_delivered_once(delayed):
    d = delayed
    d.visible_at = 0
    d.provider.complete.return_value = LLMResponse(
        content="**Observations**\n" + "The deliberate throw reached the alert pipeline. " * 100
    )
    await d.worker.once()
    ready = d.h.store.ready()[0]
    assert ready["result"].endswith("pipeline.…")
    await d.worker.once()
    text = visible_text(d.room.messages[0])
    assert len(text.encode("utf-16-le")) // 2 <= 2000
    assert "Drill sample received" in text and len(d.room.messages) == 1
    assert "pipeline.…" not in text  # Full diagnosis remains stored; drills display compactly.
    assert "[alert:" not in text


def test_redaction_happens_before_prose_budget():
    token = "phx_" + "synthetic" * 50
    text = public_prose("@everyone " + token + ". This part is public.", 100)
    assert text == "＠everyone [redacted]. This part is public."


@pytest.mark.parametrize("as_url", [True, False])
async def test_resolved_clone_is_announced_and_in_create_confirmation(ux, monkeypatch, as_url):
    clone = repository(ux.root / "dev" / "actual-clone-name")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: ux.root))
    await ux.alerts.answer(ux.current(), ux.bot, "Example")
    reply = await ux.alerts.answer(ux.current(), ux.bot, f"The repository is here: {REMOTE}" if as_url else str(clone))
    assert reply.startswith("Found the clone at `~/dev/actual-clone-name`.\n\n")
    assert "What is the PostHog project URL?" in reply
    for answer in ("https://eu.posthog.com/project/123/home", "1", "all", "no"):
        reply = await ux.alerts.answer(ux.current(), ux.bot, answer)
    assert "Repository: `~/dev/actual-clone-name`." in reply and "Reply CREATE" in reply
    assert ux.current()["config"]["repo"] == str(clone)
    ux.vault.get.assert_not_called()


async def test_failed_clone_lookup_never_claims_confirmation(ux):
    await ux.alerts.answer(ux.current(), ux.bot, "Example")
    with pytest.raises(AlertError):
        await ux.alerts.answer(ux.current(), ux.bot, REMOTE)
    assert "repo" not in ux.current()["config"]
    assert "Found the clone" not in ux.alerts.question(ux.current(), ux.bot)


def test_repository_label_outside_home_is_absolute(ux, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: ux.root / "home"))
    path = ux.root / "shared" / "clone"
    assert ux.alerts.repository_label(str(path)) == str(path)


@pytest.fixture
def access(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    monkeypatch.setattr(DiscordAlertTransport, "access_retry_delay", 0)
    h.source()
    yield h
    h.close()


@pytest.mark.parametrize("scope", ["guild", "channel"])
@pytest.mark.parametrize("error", [http_error(503, 0), http_error(429, 0), TimeoutError(), ConnectionError()])
async def test_transient_access_recovers_once_without_notice(access, scope, error):
    h = access
    method = h.bot.client.fetch_guild if scope == "guild" else h.bot.client.fetch_channel
    method.side_effect = [error, h.guild if scope == "guild" else h.room]
    await h.lifecycle.reconcile("one")
    assert method.await_count == 2
    assert not h.store.db.execute("SELECT 1 FROM lifecycle_notices").fetchone()
    assert not h.calls and h.store.channel("401")["state"] == "active"


@pytest.mark.parametrize("scope", ["guild", "channel"])
@pytest.mark.parametrize(
    "error,fragment,retries",
    [
        (http_error(403, 50001), "Discord denied permission", 1),
        (http_error(404, 10004), "could not find the server", 1),
        (http_error(503, 0), "temporarily unavailable after one retry", 2),
        (TimeoutError(), "temporarily unavailable after one retry", 2),
        (RuntimeError("private-credential-sentinel"), "failed internally", 1),
    ],
)
async def test_persistent_access_reason_is_safe_deduplicated_and_retains_resources(
    access, scope, error, fragment, retries, caplog
):
    h = access
    method = h.bot.client.fetch_guild if scope == "guild" else h.bot.client.fetch_channel
    method.side_effect = error
    for _ in range(2):
        await h.lifecycle.reconcile("one")
    notices = list(h.store.db.execute("SELECT text FROM lifecycle_notices"))
    assert len(notices) == 1 and fragment in notices[0]["text"]
    assert "(unknown)" not in notices[0]["text"]
    assert "private-credential-sentinel" not in caplog.text + notices[0]["text"]
    assert method.await_count == retries * 2
    assert not h.calls and h.store.channel("401")["state"] == "active"


async def test_specific_channel_404_still_requires_fresh_guild_check(access):
    h = access
    h.gone()
    # Initial server read succeeds; the second cannot confirm membership after its retry.
    h.bot.client.fetch_guild.side_effect = [h.guild, http_error(503, 0), http_error(503, 0)]
    await h.lifecycle.reconcile("one")
    assert h.bot.client.fetch_guild.await_count == 3
    assert not h.calls and h.store.channel("401")["state"] == "active"
    assert "after one retry" in h.store.db.execute("SELECT text FROM lifecycle_notices").fetchone()["text"]


async def test_gateway_loss_rechecks_access_before_notifying(access):
    h = access
    h.bot.client.fetch_guild.side_effect = [TimeoutError(), h.guild]
    await h.lifecycle.guild_lost("one", "301")
    assert h.bot.client.fetch_guild.await_count == 2
    assert not h.store.db.execute("SELECT 1 FROM lifecycle_notices").fetchone()
    assert not h.calls


async def test_access_cancellation_does_not_retry_or_notify(access):
    h = access
    h.bot.client.fetch_guild.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await h.lifecycle.reconcile("one")
    assert h.bot.client.fetch_guild.await_count == 1
    assert not h.store.db.execute("SELECT 1 FROM lifecycle_notices").fetchone()
