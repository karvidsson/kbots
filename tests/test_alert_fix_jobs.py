import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.alert_channels import AlertError, AlertStore
from src.core.alert_fix_store import FixJobs
from src.core.alert_fix_worker import AlertFixWorker
from tests.test_alert_lifecycle import Harness
from tests.test_alert_setup_ux import MemoryChannel, visible_text


@pytest.fixture
def setup(tmp_path):
    h = Harness(tmp_path / "state")
    source = h.source()
    source = h.store.update(
        source["id"], config={**source["config"], "repo": str(tmp_path / "registered"), "auto_fix_pr": True}
    )
    room = MemoryChannel(401, guild=h.guild)
    h.bot.client.fetch_channel = AsyncMock(return_value=room)
    yield SimpleNamespace(h=h, source=source, room=room, root=tmp_path)
    h.store.close()


async def delivered(o, *, issue=None, **flags):
    h = o.h
    h.store.receive(
        o.source,
        event_id=str(uuid.uuid4()),
        issue_id=issue or str(uuid.uuid4()),
        kind="$error_tracking_issue_created",
        message_id=str(uuid.uuid4()),
    )
    row = h.store.claim()
    h.store.annotate(row, **{"sample_drill_status": "unmarked", **flags})
    h.store.save_fix_context(
        row,
        {
            "trigger_unmarked": True,
            "tree": {"identity": "example/sample"},
            "evidence": {
                "frame_matches": [{"path": "src/handler.ts"}],
                "snippets": [{"path": "src/handler.ts", "source": "input.trim()"}],
            },
        },
    )
    h.store.save_result(row, "**Verdict**\nBad input.\n**Cause**\nMissing guard.\n**Fix**\nAdd guard.")
    ready = h.store.ready()[0]
    result = await h.alerts.transport.report(o.source, ready)
    h.store.delivered(ready, result["id"])
    stored = h.store.db.execute("SELECT * FROM receipts WHERE id=?", (row["id"],)).fetchone()
    return h.store._receipt(stored)


@pytest.mark.parametrize("flag", ["drill", "setup_test"])
async def test_drill_skips_without_claiming_real_issue(setup, flag):
    o = setup
    row = await delivered(o, **{flag: True})
    jobs = FixJobs(o.h.store)
    jobs.enqueue(o.source, row)
    assert jobs.claim() is None
    real = await delivered(o, issue=row["issue_id"])
    jobs.enqueue(o.source, real)
    assert jobs.claim()["issue_id"] == real["issue_id"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"success": False}, "diagnosis is held"),
        ({"kind": "$error_tracking_issue_spiking"}, "only created"),
        ({"fix_context": {"trigger_unmarked": False}}, "classification"),
        ({"fix_context": {"trigger_unmarked": True, "evidence": {}}}, "no tracked"),
    ],
)
async def test_skip_gate_is_durable(setup, change, reason):
    row = {**await delivered(setup), **change}
    jobs = FixJobs(setup.h.store)
    jobs.enqueue(setup.source, row)
    assert jobs.claim() is None
    job = jobs.get(setup.h.store.db.execute("SELECT fix_id FROM alert_fix_cards").fetchone()[0])
    assert reason in job["result"]["reason"]


async def test_issue_dedupe_and_lease_survive_store_reopen(setup):
    o = setup
    first = await delivered(o)
    second = await delivered(o, issue=first["issue_id"])
    jobs = FixJobs(o.h.store)
    jobs.enqueue(o.source, first)
    jobs.enqueue(o.source, second)
    assert o.h.store.db.execute("SELECT count(*) FROM alert_fixes").fetchone()[0] == 1
    claimed = jobs.claim(now=1000)
    assert jobs.claim(now=1001) is None
    second_store = AlertStore(o.h.directory)
    try:
        restarted = FixJobs(second_store)
        assert restarted.claim(now=1002) is None
        resumed = restarted.claim(now=4001)
        assert resumed["id"] == claimed["id"] and resumed["lease"] != claimed["lease"]
        with pytest.raises(AlertError):
            jobs.guard(claimed)
    finally:
        second_store.close()


async def test_budget_is_registration_scoped_and_not_charged_again_on_resume(setup):
    o = setup
    jobs = FixJobs(o.h.store, limit=1)
    for _ in range(2):
        jobs.enqueue(o.source, await delivered(o))
    first = jobs.claim()
    assert first
    assert jobs.claim() is None
    assert o.h.store.db.execute("SELECT count(*) FROM alert_fixes WHERE state='failed'").fetchone()[0] == 1
    resumed = jobs.claim(now=time.time() + 2401)
    assert resumed["id"] == first["id"]


async def test_queued_writing_ready_edits_same_committed_message(setup):
    o = setup
    row = await delivered(o)
    worker = AlertFixWorker(o.h.store, o.h.manager, o.h.alerts.transport, o.root)
    worker.accounts.add("one")
    worker.jobs.enqueue(o.source, row)
    await worker.notices()
    job = worker.jobs.claim()
    await worker.notices()
    assert "Writing fix" in visible_text(o.room.messages[0])
    worker.jobs.save(
        job,
        state="complete",
        pr={
            "number": 17,
            "url": "https://github.com/example/sample/pull/17",
            "state": "open",
            "draft": False,
            "merged": False,
        },
    )
    await worker.notices()
    assert len(o.room.messages) == 1 and "PR #17 ready" in visible_text(o.room.messages[0])
    before = len(o.room.edits)
    await worker.notices()
    assert len(o.room.edits) == before


async def test_revocation_blocks_publication_and_card_updates(setup):
    o = setup
    jobs = FixJobs(o.h.store)
    jobs.enqueue(o.source, await delivered(o))
    job = jobs.claim()
    o.h.store.disable(o.source["id"])
    with pytest.raises(AlertError):
        jobs.save(job, state="complete", pr={})
    assert jobs.cards({"one"}) == []


async def test_uncertain_create_is_reconciled_without_another_post(setup, monkeypatch):
    o = setup
    row = await delivered(o)
    worker = AlertFixWorker(o.h.store, o.h.manager, o.h.alerts.transport, o.root)
    worker.jobs.enqueue(o.source, row)
    job = worker.jobs.claim()
    tree = {"identity": "example/sample"}
    worker.jobs.save(job, tree=tree, pr_intent=True)
    monkeypatch.setattr(
        "src.core.alert_fix_worker.registered_remote",
        lambda p: ("https://github.com/example/sample.git", "example/sample"),
    )
    worker.publisher.find = lambda *args: None
    worker.publisher.create = lambda *args: pytest.fail("Do not repeat uncertain PR creation")
    await worker.execute(job)
    assert worker.jobs.get(job["id"])["state"] == "failed"
    assert "uncertain" in worker.jobs.get(job["id"])["result"]["reason"]


async def test_delayed_queue_counts_run_start_not_receipt_creation(setup):
    o = setup
    jobs = FixJobs(o.h.store, limit=1)
    jobs.enqueue(o.source, await delivered(o))
    o.h.store.db.execute("UPDATE alert_fixes SET created=?", (time.time() - 172800,))
    assert jobs.claim()
    jobs.enqueue(o.source, await delivered(o))
    assert jobs.claim() is None
    assert o.h.store.db.execute("SELECT count(*) FROM alert_fixes WHERE state='failed'").fetchone()[0] == 1


async def test_existing_foreign_head_is_not_labeled_as_our_tested_ready_pr(setup):
    o = setup
    worker = AlertFixWorker(o.h.store, o.h.manager, o.h.alerts.transport, o.root)
    worker.accounts.add("one")
    worker.jobs.enqueue(o.source, await delivered(o))
    job = worker.jobs.claim()
    worker.jobs.save(job, commit="a" * 40, branch="alert-fix/a-b")
    worker.existing_pr(
        job,
        {
            "number": 5,
            "url": "https://github.com/example/sample/pull/5",
            "head": "b" * 40,
            "branch": "manual-fix",
            "state": "open",
            "merged": False,
            "draft": False,
        },
    )
    await worker.notices()
    assert "Existing PR #5 open" in visible_text(o.room.messages[0])
    assert "ready" not in visible_text(o.room.messages[0])
