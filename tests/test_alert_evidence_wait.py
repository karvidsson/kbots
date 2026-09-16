"""Ingestion lag must not spend a diagnosis before stack evidence can be read."""

import asyncio
import copy
import json
import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from extras.posthog.alerts import sampled_exception
from src.core import alert_diagnosis
from src.core.alert_channels import AlertError, AlertStore
from src.core.base import LLMResponse
from tests.test_alert_exception_evidence import response
from tests.test_alert_lifecycle import Harness
from tests.test_alert_setup_ux import MemoryChannel
from tests.test_posthog_alerts import FakeResponse, FakeSession


@pytest.fixture
def delayed(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(alert_diagnosis.time, "time", lambda: clock.now)
    h = Harness(tmp_path / "state")
    room = MemoryChannel(401, guild=h.guild)
    h.bot.client.fetch_channel = AsyncMock(return_value=room)
    repo = tmp_path / "repo"
    handler = repo / "server/api/debug/boom.get.ts"
    handler.parent.mkdir(parents=True)
    handler.write_text("// Deliberate drill route\nthrow new Error('pipeline drill');\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "fixture"],
        cwd=repo,
        check=True,
    )
    source = h.source()
    source = h.store.update(source["id"], config={**source["config"], "repo": str(repo)})
    issue_id = str(uuid.uuid4())
    h.store.receive(
        source,
        event_id=str(uuid.uuid4()),
        issue_id=issue_id,
        kind="$error_tracking_issue_created",
        message_id="600",
    )
    provider = SimpleNamespace(
        supports_tool_free=True,
        complete=AsyncMock(return_value=LLMResponse(content="Observed throw in server/api/debug/boom.get.ts.")),
    )
    manager = SimpleNamespace(
        agent_configs={"worker": {"llm": {"model": "fixture"}}},
        defaults={},
        storage=None,
        _apply_provider_override=lambda *args: None,
        _get_agent_llm=lambda *args: provider,
        _effective_model=lambda *args: "fixture",
        active_turns=0,
    )
    raw = response()
    frames = raw["results"][0]["properties"]["$exception_list"][0]["stacktrace"]["frames"]
    # Exact two frame shapes reported live, including the unresolved name.
    frames[:] = [
        {
            "source": ".output/server/chunks/routes/api/debug/boom.get.mjs",
            "resolved_name": name,
            "line": line,
            "in_app": True,
        }
        for name, line in (("handler", 4), ("?", 6))
    ]
    result = SimpleNamespace(
        h=h,
        clock=clock,
        source=source,
        issue_id=issue_id,
        room=room,
        provider=provider,
        manager=manager,
        raw=raw,
        visible_at=1030,
        reads=[],
    )

    async def request(config, method, resource, **kwargs):
        # Use the actual request budget even though the HTTP transport is offline.
        h.adapter._budget(config["host"])
        result.reads.append((clock.now, method, resource, kwargs))
        if method == "GET":
            return {"id": issue_id, "name": "Error", "first_seen": "2026-09-16T19:47:53Z"}
        assert resource == "error_tracking/query/issue_events/"
        assert h.adapter._sample_request_allowed(resource, kwargs["payload"])
        return copy.deepcopy(raw) if clock.now >= result.visible_at else {"results": []}

    h.adapter._request = request
    result.worker = alert_diagnosis.AlertWorker(
        h.store,
        {"posthog": h.adapter},
        manager,
        h.alerts.transport,
        str(tmp_path),
    )
    yield result
    h.store.close()


def restart(d):
    directory = d.h.store.path.parent
    d.h.store.close()
    d.h.store = AlertStore(directory)
    d.h.alerts.transport.store = d.h.store
    d.h.adapter.store = d.h.store
    d.worker = alert_diagnosis.AlertWorker(
        d.h.store,
        {"posthog": d.h.adapter},
        d.manager,
        d.h.alerts.transport,
        str(directory.parent),
    )


def diagnosis_uses(d):
    return d.h.store.db.execute(
        "SELECT sum(used) FROM budgets WHERE scope=?", ("diagnosis:" + d.source["id"],)
    ).fetchone()[0]


async def test_late_stack_waits_across_restart_and_selects_real_handler(delayed):
    d = delayed
    for moment, next_time in ((1000, 1005), (1005, 1015), (1015, 1030)):
        d.clock.now = moment
        assert await d.worker.once()
        d.provider.complete.assert_not_awaited()
        pending = d.h.store.pending()[0]
        assert pending["available"] == next_time
        assert pending["evidence"]["deadline"] == 1090
        assert pending["attempts"] == 0
        assert diagnosis_uses(d) == 1
        assert "Waiting for stack trace" in d.room.messages[0].content
        assert "12 diagnoses" not in d.room.messages[0].content
        assert not d.worker.running and d.manager.active_turns == 0
        assert all(not lock.locked() for lock in d.h.alerts.transport.message_locks.values())
        assert d.worker.store.next_delay() == min(15, next_time - moment)
        restart(d)
        assert not await d.worker.once()  # Restart cannot ignore the scheduled delay.
    d.clock.now = 1030
    assert await d.worker.once()
    d.provider.complete.assert_awaited_once()
    payload = json.loads(d.provider.complete.call_args.args[0][1].content)
    assert payload["repository"]["selection"] == "in-app frame paths"
    assert [s["path"] for s in payload["repository"]["snippets"]] == ["server/api/debug/boom.get.ts"]
    assert payload["issue"]["sample"]["exceptions"][0]["frames"][0]["resolved_name"] == "?"
    assert "private-person" not in json.dumps(payload)
    assert "setup_test" not in json.dumps(payload)
    receipt = d.h.store.ready()[0]
    assert receipt["sample_drill_status"] == "drill" and receipt["attempts"] == 1
    assert receipt["evidence"]["status"] == "available"
    assert diagnosis_uses(d) == 1
    assert len(d.reads) == 9  # Three empty GET+POSTs, then GET+POST+fixed filtered POST.
    assert (
        d.h.store.db.execute(
            "SELECT used FROM budgets WHERE scope=? AND bucket=?",
            ("posthog:https://eu.posthog.com:60", int(d.clock.now // 60)),
        ).fetchone()[0]
        > 0
    )
    await d.worker.once()
    assert len(d.room.messages) == 1 and "boom.get.ts" in d.room.messages[0].content


async def test_deadline_fallback_is_durable_and_does_not_claim_no_events(delayed):
    d = delayed
    d.visible_at = float("inf")
    for moment in (1000, 1005, 1015, 1030, 1050, 1080):
        d.clock.now = moment
        assert await d.worker.once()
        d.provider.complete.assert_not_awaited()
    assert d.h.store.pending()[0]["available"] == 1090
    assert diagnosis_uses(d) == 1
    restart(d)
    d.clock.now = 1089
    assert not await d.worker.once()
    d.clock.now = 1090
    assert await d.worker.once()
    d.provider.complete.assert_awaited_once()
    assert len(d.reads) == 12 and diagnosis_uses(d) == 1
    payload = json.loads(d.provider.complete.call_args.args[0][1].content)
    assert "indexing delay and no matching events cannot be distinguished" in payload["issue"]["sample"]["status"]
    receipt = d.h.store.ready()[0]
    assert receipt["evidence"]["status"] == "timed_out"
    assert receipt["result"].startswith("Stack trace was not yet available after a 90-second wait.")
    assert "limited issue details" in receipt["result"]


async def test_wait_does_not_block_other_receipts_or_lose_budget_reservation(delayed):
    d = delayed
    await d.worker.once()
    d.h.store.receive(
        d.source,
        event_id=str(uuid.uuid4()),
        issue_id=str(uuid.uuid4()),
        kind="$error_tracking_issue_created",
        message_id="601",
    )
    # Only the waiting receipt has a reserved diagnosis. A thirteenth new one
    # must defer without postponing that receipt until the next hour.
    d.h.store.db.execute("UPDATE budgets SET used=12 WHERE scope=?", ("diagnosis:" + d.source["id"],))
    assert not await d.worker.once()
    pending = d.h.store.pending()
    assert sorted(r["available"] for r in pending) == [1005, 3600]
    d.clock.now = d.visible_at = 1005
    assert await d.worker.once()
    d.provider.complete.assert_awaited_once()
    assert diagnosis_uses(d) == 12


async def test_another_receipt_can_run_during_evidence_wait(delayed):
    d = delayed
    await d.worker.once()
    second = d.h.source(channel_id="402")
    second = d.h.store.update(second["id"], config={**second["config"], "repo": d.source["config"]["repo"]})
    d.h.store.receive(
        second,
        event_id=str(uuid.uuid4()),
        issue_id=d.issue_id,
        kind="$error_tracking_issue_created",
        message_id="601",
    )
    d.visible_at = 1000
    assert await d.worker.once()
    assert d.h.store.ready()[0]["source_id"] == second["id"]
    assert d.h.store.pending()[0]["source_id"] == d.source["id"]
    d.provider.complete.assert_awaited_once()


@pytest.mark.parametrize("action", ["disable", "rotate", "request_teardown"])
async def test_revocation_cancels_waiting_receipt(delayed, action):
    d = delayed
    await d.worker.once()
    if action == "request_teardown":
        d.h.store.request_teardown(d.source["id"], "deleted")
    else:
        getattr(d.h.store, action)(d.source["id"])
    d.clock.now = 1030
    assert not await d.worker.once()
    d.provider.complete.assert_not_awaited()
    assert len(d.reads) == 2


async def test_issue_not_found_is_held_without_evidence_retries(delayed):
    d = delayed

    async def missing(*args, **kwargs):
        raise AlertError("PostHog could not find this issue (HTTP 404); diagnosis is held")

    d.h.adapter._request = missing
    await d.worker.once()
    receipt = d.h.store.ready()[0]
    assert not receipt["success"] and "HTTP 404" in receipt["result"]
    assert not d.h.store.pending()
    d.provider.complete.assert_not_awaited()


async def test_http_404_is_distinct_from_an_empty_sample(delayed):
    d = delayed
    # Exercise the actual transport status branch, not the offline request stub.
    from extras.posthog.alerts import PostHogAdapter

    session = FakeSession(FakeResponse(404, [b"untrusted vendor detail"]))
    d.h.adapter.session_factory = lambda: session
    with pytest.raises(AlertError, match="could not find this issue \\(HTTP 404\\)"):
        await PostHogAdapter._request(
            d.h.adapter,
            d.source["config"],
            "GET",
            f"error_tracking/issues/{d.issue_id}/",
        )


async def test_slow_read_is_bounded_and_without_any_response_is_held(delayed, monkeypatch):
    d = delayed
    remaining = []
    original = asyncio.wait_for

    async def deadline(awaitable, *, timeout):
        if timeout == 90:
            remaining.append(timeout)
            awaitable.close()
            raise TimeoutError
        return await original(awaitable, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", deadline)
    await d.worker.once()
    assert remaining == [90]
    assert "timed out before a sample response" in d.h.store.ready()[0]["result"]
    d.provider.complete.assert_not_awaited()


async def test_model_retry_uses_checkpointed_stack_without_new_vendor_read(delayed):
    d = delayed
    d.visible_at = 1000
    d.provider.complete.side_effect = asyncio.CancelledError
    await d.worker.once()  # Per-source cancellation leaves the durable lease recoverable.
    assert d.h.store.counts(d.source["id"]) == {"running": 1}
    assert len(d.reads) == 3
    restart(d)
    d.provider.complete.side_effect = None
    d.clock.now = 1361  # Existing interrupted-running lease recovery, not a new wait window.
    await d.worker.once()
    assert len(d.reads) == 3
    assert d.h.store.ready()[0]["attempts"] == 2
    assert diagnosis_uses(d) == 2


async def test_interrupted_evidence_read_resumes_checkpoint_without_early_diagnosis(delayed):
    d = delayed
    await d.worker.once()
    d.clock.now = 1005
    d.h.adapter.issue = AsyncMock(side_effect=asyncio.CancelledError)
    await d.worker.once()  # Per-source cancellation leaves the durable lease recoverable.
    assert d.h.store.counts(d.source["id"]) == {"running": 1}
    restart(d)
    assert not await d.worker.once()
    d.provider.complete.assert_not_awaited()
    d.clock.now = 1366
    await d.worker.once()
    assert d.h.store.ready()[0]["evidence"]["deadline"] == 1090
    assert d.h.store.ready()[0]["evidence"]["status"] == "timed_out"
    d.provider.complete.assert_awaited_once()


async def test_internal_flag_is_not_model_input_or_user_output(delayed):
    d = delayed
    d.visible_at = 1000
    d.provider.complete.return_value = LLMResponse(content="setup_test is false. Evidence indicates a declared drill.")
    await d.worker.once()
    assert "setup_test" not in json.dumps([m.content for m in d.provider.complete.call_args.args[0]])
    result = d.h.store.ready()[0]["result"]
    assert "setup_test" not in result and "This is not a setup check" in result


def test_empty_sample_is_an_observation_not_a_no_events_claim():
    sample = sampled_exception({"results": []})
    assert sample["availability"] == "empty"
    assert "indexing delay or no matching events" in sample["status"]


def test_wait_schema_migration_is_idempotent_and_preserves_prior_receipts(tmp_path):
    store = AlertStore(tmp_path)
    source = store.begin("worker", "101", "one", "201")
    source = store.update(source["id"], state="active")
    store.receive(source, event_id=str(uuid.uuid4()), issue_id=str(uuid.uuid4()), kind="created", message_id="600")
    before = dict(store.db.execute("SELECT * FROM receipts").fetchone())
    store.db.execute("ALTER TABLE receipts DROP COLUMN evidence")
    store.db.execute("ALTER TABLE receipts DROP COLUMN evidence_retry")
    store.close()
    for _ in range(2):
        store = AlertStore(tmp_path)
        after = dict(store.db.execute("SELECT * FROM receipts").fetchone())
        assert after == before
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        store.close()


async def test_available_exception_without_app_frames_does_not_wait_forever(delayed):
    d = delayed
    d.visible_at = 1000
    d.raw["results"][0]["properties"]["$exception_list"][0]["stacktrace"]["frames"] = []
    await d.worker.once()
    d.provider.complete.assert_awaited_once()
    assert not d.h.store.pending()
    payload = json.loads(d.provider.complete.call_args.args[0][1].content)
    assert payload["repository"]["selection"].startswith("issue-name fallback")
    assert payload["issue"]["sample"]["availability"] == "available"


@pytest.mark.parametrize("reason", ["PostHog request budget exhausted; retry later", "PostHog returned HTTP 503"])
async def test_read_failures_are_held_instead_of_being_treated_as_empty(delayed, reason):
    d = delayed
    d.h.adapter.issue = AsyncMock(side_effect=AlertError(reason))
    await d.worker.once()
    receipt = d.h.store.ready()[0]
    assert not receipt["success"] and reason in receipt["result"]
    assert not d.h.store.pending()
    d.provider.complete.assert_not_awaited()


async def test_deadline_is_persisted_before_request_and_stale_lease_cannot_defer(delayed):
    d = delayed
    original = d.h.adapter.issue

    async def inspect(*args):
        row = d.h.store.db.execute("SELECT * FROM receipts").fetchone()
        state = json.loads(row["evidence"])
        assert state["deadline"] == 1090 and state["status"] == "waiting"
        assert d.h.store.db.in_transaction is False
        return await original(*args)

    d.h.adapter.issue = inspect
    await d.worker.once()
    d.clock.now = 1005
    stale = d.h.store.claim(lease_seconds=1)
    d.clock.now = 1007
    fresh = d.h.store.claim()
    assert not d.h.store.defer_evidence(stale, {"sample": {"availability": "empty"}})
    assert not d.h.store.checkpoint_evidence(stale, {"status": "available"})
    assert d.h.store.checkpoint_evidence(fresh, fresh["evidence"])
    assert diagnosis_uses(d) == 2  # A genuine interrupted lease remains charged.
