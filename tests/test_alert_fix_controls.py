"""Real Discord views and durable owner intent around the fix worker boundary."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from src.connectors.alert_fix_controls import FixControls
from src.core.alert_channels import AlertStore
from src.core.alert_fix_worker import AlertFixWorker
from tests.test_alert_fix_jobs import delivered
from tests.test_alert_fix_jobs import setup as setup_fixture
from tests.test_alert_setup_ux import visible_text

setup = setup_fixture


def manual(o):
    o.source = o.h.store.update(o.source["id"], config={**o.source["config"], "auto_fix_pr": False})
    o.h.alerts.fixer.accounts.add("one")
    return o.h.alerts.fix_controls


def interaction(o, user="101", *, bot=False):
    return SimpleNamespace(
        user=SimpleNamespace(id=int(user), bot=bot),
        client=o.h.bot.client,
        message=o.room.messages[-1],
        channel_id=401,
        guild_id=301,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def button(control, o, receipt):
    return control.view(o.h.store.get(o.source["id"]), receipt).children[0]


async def test_manual_card_has_persistent_button_without_starting_a_run(setup):
    o = setup
    controls = manual(o)
    r = await delivered(o)
    assert not await o.h.alerts.fixer.once()
    b = button(controls, o, r)
    assert b.label == "Fix it" and not b.disabled
    assert o.source["id"] in b.custom_id and r["issue_id"] in b.custom_id and len(b.custom_id) <= 100
    assert controls.view(o.source, r).is_persistent()
    assert o.h.store.db.execute("SELECT count(*) FROM alert_fix_runs").fetchone()[0] == 0
    assert len(o.room.messages) == 1 and o.room.messages[0].components


@pytest.mark.parametrize("user,is_bot", [("102", False), ("101", True)])
async def test_only_setup_owner_can_click_even_if_another_admin(setup, user, is_bot):
    o = setup
    controls = manual(o)
    r = await delivered(o)
    o.h.connector._admin_users = ["101", "102"]
    i = interaction(o, user, bot=is_bot)
    await button(controls, o, r).callback(i)
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True
    assert "Only the person" in i.response.send_message.call_args.args[0]
    assert o.h.store.db.execute("SELECT count(*) FROM alert_fixes").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["message", "channel", "guild", "client", "revision", "disabled"])
async def test_button_refuses_wrong_or_stale_card_binding(setup, change):
    o = setup
    controls = manual(o)
    r = await delivered(o)
    i = interaction(o)
    if change == "message":
        i.message = SimpleNamespace(**{**vars(i.message), "id": 9999})
    elif change == "channel":
        i.channel_id = 999
    elif change == "guild":
        i.guild_id = 999
    elif change == "client":
        i.client = object()
    elif change == "revision":
        o.h.store.db.execute("UPDATE sources SET revision=2 WHERE id=?", (o.source["id"],))
    else:
        o.h.store.disable(o.source["id"])
    await controls.click(o.source["id"], r["id"], r["issue_id"], i)
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True
    assert o.h.store.db.execute("SELECT count(*) FROM alert_fixes").fetchone()[0] == 0


async def test_duplicate_clicks_disable_one_run_and_no_pr_restores_retry(setup):
    o = setup
    controls = manual(o)
    r = await delivered(o)
    jobs = controls.jobs
    await asyncio.gather(*(button(controls, o, r).callback(interaction(o)) for _ in range(2)))
    assert o.h.store.db.execute("SELECT count(*) FROM alert_fixes").fetchone()[0] == 1
    assert button(controls, o, r).disabled
    job = jobs.claim()
    assert job and job["payload"]["manual"]
    jobs.save(job, state="failed", reason="repository gate failed")
    await o.h.alerts.fixer.notices()
    assert not button(controls, o, r).disabled
    i = interaction(o)
    await button(controls, o, r).callback(i)
    again = jobs.claim()
    assert again["id"] == job["id"] and again["cycle"] > job["cycle"]
    assert o.h.store.db.execute("SELECT count(*) FROM alert_fix_runs").fetchone()[0] == 2
    jobs.save(
        again,
        state="complete",
        pr={
            "number": 7,
            "url": "https://github.com/example/sample/pull/7",
            "state": "open",
            "draft": False,
            "merged": False,
        },
    )
    # A finished repair replaces the control with a link to its PR, so the card
    # cannot be clicked into a second run and shows the outcome at a glance.
    await o.h.alerts.fixer.notices()
    link = button(controls, o, r)
    assert link.label == "PR #7" and link.url == "https://github.com/example/sample/pull/7"
    assert link.custom_id is None and link.style is discord.ButtonStyle.link
    assert jobs.claim() is None and len(o.room.messages) == 1


async def test_manual_retry_uses_same_daily_cap_and_restores_button_on_limit(setup):
    o = setup
    controls = manual(o)
    controls.jobs.limit = 1
    r = await delivered(o)
    await button(controls, o, r).callback(interaction(o))
    job = controls.jobs.claim()
    controls.jobs.save(job, state="failed", reason="no candidate")
    await button(controls, o, r).callback(interaction(o))
    assert controls.jobs.claim() is None
    assert not button(controls, o, r).disabled
    await o.h.alerts.fixer.notices()
    assert "daily fix-run limit" in visible_text(o.room.messages[0])


async def test_uncertain_publication_survives_manual_retry_rate_limit_and_reconciles_only(setup, monkeypatch):
    import time

    o = setup
    controls = manual(o)
    controls.jobs.limit = 1
    r = await delivered(o)
    await button(controls, o, r).callback(interaction(o))
    job = controls.jobs.claim()
    controls.jobs.save(
        job, state="failed", tree={"identity": "example/sample"}, pr_intent=True, reason="acknowledgement lost"
    )
    await button(controls, o, r).callback(interaction(o))
    assert controls.jobs.claim() is None
    assert controls.jobs.get(job["id"])["result"]["pr_intent"]
    o.h.store.db.execute("UPDATE alert_fix_runs SET started=?", (time.time() - 86401,))
    await button(controls, o, r).callback(interaction(o))
    resumed = controls.jobs.claim()
    assert resumed and resumed["result"]["pr_intent"]
    monkeypatch.setattr(
        "src.core.alert_fix_worker.registered_remote",
        lambda p: ("https://github.com/example/sample.git", "example/sample"),
    )
    worker = o.h.alerts.fixer
    worker.publisher.find = lambda *args: None
    worker.publisher.create = lambda *args: pytest.fail("An uncertain creation must never repeat")
    worker.repositories.checkout = lambda *args: pytest.fail("An uncertain creation must not rerun the model")
    await worker.execute(resumed)
    assert controls.jobs.get(job["id"])["state"] == "failed"
    assert controls.jobs.get(job["id"])["result"]["pr_intent"]


async def test_explicit_click_can_search_without_resolved_frame_but_auto_cannot(setup):
    o = setup
    controls = manual(o)
    r = await delivered(o)
    r["fix_context"]["evidence"] = {}
    import json

    o.h.store.db.execute("UPDATE receipts SET fix_context=? WHERE id=?", (json.dumps(r["fix_context"]), r["id"]))
    controls.jobs.enqueue(o.source, r)
    assert controls.jobs.claim() is None
    await button(controls, o, r).callback(interaction(o))
    job = controls.jobs.claim()
    assert job and job["payload"]["manual"]
    assert job["payload"]["context"]["evidence"] == {}


@pytest.mark.parametrize("flag", ["drill", "setup_test", "sample_drill_status"])
async def test_drill_has_no_control_and_forged_click_cannot_enqueue(setup, flag):
    o = setup
    controls = manual(o)
    r = await delivered(o, **{flag: "drill" if flag == "sample_drill_status" else True})
    assert controls.view(o.source, r) is None
    await controls.click(o.source["id"], r["id"], r["issue_id"], interaction(o))
    assert controls.jobs.claim() is None


async def test_database_reopen_restores_real_persistent_view_and_disabled_state(setup):
    o = setup
    controls = manual(o)
    r = await delivered(o)
    await button(controls, o, r).callback(interaction(o))
    job = controls.jobs.claim()
    o.h.store.close()
    o.h.store = AlertStore(o.h.directory)
    o.h.alerts.store = o.h.store
    o.h.alerts.transport.store = o.h.store
    o.h.alerts.fixer = AlertFixWorker(o.h.store, o.h.manager, o.h.alerts.transport, o.root)
    o.h.alerts.fix_controls = FixControls(o.h.alerts)
    o.h.alerts.transport.fix_controls = o.h.alerts.fix_controls
    client = discord.Client(intents=discord.Intents.none())
    o.h.bot.client.add_view = client.add_view
    o.h.alerts.fix_controls.restore("one")
    assert len(client.persistent_views) == 1 and client.persistent_views[0].children[0].disabled
    assert o.h.alerts.fixer.jobs.claim() is None
    assert o.h.alerts.fixer.jobs.get(job["id"])["lease"] == job["lease"]
    await client.close()


async def test_migration_defaults_false_and_settings_update_without_resources_or_revision(setup):
    o = setup
    o.h.store.update(o.source["id"], config={k: v for k, v in o.source["config"].items() if k != "auto_fix_pr"})
    second = AlertStore(o.h.directory)
    assert second.get(o.source["id"])["config"]["auto_fix_pr"] is False
    second.close()
    controls = manual(o)
    r = await delivered(o)
    source = o.h.store.get(o.source["id"])
    controls.jobs.enqueue(source, r)
    i = interaction(o)
    await controls.setting(o.h.bot, i, True, source["id"])
    changed = o.h.store.get(source["id"])
    assert changed["config"]["auto_fix_pr"] is True and changed["revision"] == source["revision"]
    assert changed["channel_id"] == source["channel_id"] and changed["webhook_id"] == source["webhook_id"]
    assert controls.view(changed, r) is None and controls.jobs.claim()
    denied = interaction(o, "102")
    await controls.setting(o.h.bot, denied, False, source["id"])
    assert o.h.store.get(source["id"])["config"]["auto_fix_pr"] is True


async def test_retained_preupgrade_card_gets_button_and_uses_saved_trigger_evidence(setup):
    import json

    o = setup
    controls = manual(o)
    r = await delivered(o)
    evidence = {"issue": {"sample": {"matches_trigger": True, "drill_status": "unmarked"}}}
    o.h.store.db.execute("UPDATE receipts SET fix_context='{}',evidence=? WHERE id=?", (json.dumps(evidence), r["id"]))
    assert not await o.h.alerts.fixer.once()
    r = controls.receipt(r["id"])
    assert not button(controls, o, r).disabled
    await button(controls, o, r).callback(interaction(o))
    job = controls.jobs.claim()
    assert job and job["payload"]["manual"] and job["payload"]["context"]["trigger_unmarked"]


async def test_switching_off_stops_unstarted_auto_job_and_preserves_manual_intent(setup):
    o = setup
    controls = o.h.alerts.fix_controls
    controls.jobs.enqueue(o.source, await delivered(o))
    o.source = o.h.store.update(o.source["id"], config={**o.source["config"], "auto_fix_pr": False})
    assert controls.jobs.claim() is None
    row = o.h.store.db.execute("SELECT * FROM receipts").fetchone()
    receipt = o.h.store._receipt(row)
    await button(controls, o, receipt).callback(interaction(o))
    job = controls.jobs.claim()
    assert job and job["payload"]["manual"]


async def test_unknown_trigger_cannot_be_promoted_by_button(setup):
    import json

    o = setup
    controls = manual(o)
    r = await delivered(o)
    r["fix_context"]["trigger_unmarked"] = False
    o.h.store.db.execute("UPDATE receipts SET fix_context=? WHERE id=?", (json.dumps(r["fix_context"]), r["id"]))
    await button(controls, o, r).callback(interaction(o))
    assert controls.jobs.claim() is None
    assert not button(controls, o, r).disabled
    assert "classification" in controls.jobs.for_receipt(r)["result"]["reason"]


async def test_legacy_setup_event_without_presentation_flag_never_gets_button_or_auto_fix(setup):
    import uuid

    o = setup
    r = await delivered(o)
    event = str(uuid.uuid5(uuid.UUID(o.source["id"]), o.source["nonce"]))
    o.h.store.db.execute("UPDATE receipts SET event_id=? WHERE id=?", (event, r["id"]))
    r = o.h.alerts.fix_controls.receipt(r["id"])
    assert o.h.alerts.fix_controls.drill(o.source, r)
    o.h.alerts.fixer.jobs.enqueue(o.source, r)
    assert o.h.alerts.fixer.jobs.claim() is None
    o.source = o.h.store.update(o.source["id"], config={**o.source["config"], "auto_fix_pr": False})
    assert o.h.alerts.fix_controls.view(o.source, r) is None


async def test_button_label_shows_whether_a_repair_ran(setup):
    o = setup
    controls = manual(o)
    r = await delivered(o)
    assert button(controls, o, r).label == "Fix it"
    await button(controls, o, r).callback(interaction(o))
    await o.h.alerts.fixer.notices()
    queued = button(controls, o, r)
    assert queued.label == "Writing fix…" and queued.disabled
    job = controls.jobs.claim()
    controls.jobs.save(job, state="failed", reason="repository gate failed")
    await o.h.alerts.fixer.notices()
    retry = button(controls, o, r)
    assert retry.label == "Fix it again" and not retry.disabled
