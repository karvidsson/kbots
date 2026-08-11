"""Scheduler: cron matching, due-detection (no double-fire), tools, dispatch."""

from datetime import datetime

import pytest

from src.core import schedules as sched
from src.core.base import ToolContext
from src.tools.schedule_admin import cancel_schedule, list_schedules, schedule_task


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    (tmp_path / "config").mkdir()
    return tmp_path


# --- cron ---

def test_cron_matches():
    # daily 08:00
    assert sched.cron_matches("0 8 * * *", datetime(2026, 7, 4, 8, 0))
    assert not sched.cron_matches("0 8 * * *", datetime(2026, 7, 4, 8, 1))
    assert not sched.cron_matches("0 8 * * *", datetime(2026, 7, 4, 9, 0))
    # every 15 min via step
    assert sched.cron_matches("*/15 * * * *", datetime(2026, 7, 4, 10, 30))
    assert not sched.cron_matches("*/15 * * * *", datetime(2026, 7, 4, 10, 31))
    # weekdays 9am — 2026-07-06 is a Monday, 2026-07-04 a Saturday
    assert sched.cron_matches("0 9 * * 1-5", datetime(2026, 7, 6, 9, 0))
    assert not sched.cron_matches("0 9 * * 1-5", datetime(2026, 7, 4, 9, 0))
    # sunday via 0 and via 7
    assert sched.cron_matches("0 9 * * 0", datetime(2026, 7, 5, 9, 0))
    assert sched.cron_matches("0 9 * * 7", datetime(2026, 7, 5, 9, 0))
    # list of hours
    assert sched.cron_matches("30 8,12,18 * * *", datetime(2026, 7, 4, 12, 30))


def test_is_valid_cron():
    assert sched.is_valid_cron("0 8 * * *")
    assert not sched.is_valid_cron("0 8 * *")      # 4 fields
    assert not sched.is_valid_cron("bad cron here now")


# --- due detection ---

def test_every_due(overlay):
    r = sched.create_schedule("a", "c", "do", "u", spec_type="every", spec="60", now=1000.0)
    assert sched.due_schedules(1030.0) == []          # 30s < 60s
    fired = sched.due_schedules(1061.0)               # 61s >= 60s
    assert len(fired) == 1 and fired[0]["id"] == r["id"]
    assert sched.due_schedules(1070.0) == []          # last_run advanced, not due again


def test_once_fires_and_disables(overlay):
    sched.create_schedule("a", "c", "do", "u", spec_type="once", spec="2000", now=1000.0)
    assert sched.due_schedules(1999.0) == []
    assert len(sched.due_schedules(2001.0)) == 1
    assert sched.due_schedules(2100.0) == []          # disabled after firing
    assert sched.list_schedules("a")[0]["enabled"] is False


def test_cron_fires_once_per_minute(overlay):
    # 08:00 daily
    sched.create_schedule("a", "c", "do", "u", spec_type="cron", spec="0 8 * * *", now=0.0)
    t = datetime(2026, 7, 4, 8, 0, 5).timestamp()     # 08:00:05
    assert len(sched.due_schedules(t)) == 1
    assert sched.due_schedules(t + 20) == []          # still 08:00, already fired this minute


def test_killswitch_blocks_due(overlay):
    sched.create_schedule("a", "c", "do", "u", spec_type="every", spec="30", now=1000.0)
    sched.set_enabled(False)
    assert sched.due_schedules(1100.0) == []


def test_validation(overlay):
    with pytest.raises(ValueError, match="Invalid cron"):
        sched.create_schedule("a", "c", "do", "u", spec_type="cron", spec="nope", now=0.0)
    with pytest.raises(ValueError, match=">= 30"):
        sched.create_schedule("a", "c", "do", "u", spec_type="every", spec="10", now=0.0)


# --- tools ---

async def test_schedule_task_tool_cron(overlay):
    ctx = ToolContext(agent_id="atlas", channel_id="chan1", user_id="u1")
    out = await schedule_task(ctx, "check the site", cron="0 8 * * *")
    assert "Scheduled" in out and "0 8 * * *" in out
    assert sched.list_schedules("atlas")[0]["instruction"] == "check the site"


async def test_schedule_task_requires_one_spec(overlay):
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="u")
    assert (await schedule_task(ctx, "x")).startswith("ERROR")             # none
    assert (await schedule_task(ctx, "x", cron="0 8 * * *", in_minutes=5)).startswith("ERROR")  # two


async def test_list_and_cancel(overlay):
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="u")
    await schedule_task(ctx, "ping", every_minutes=5)
    assert "every 5min" in await list_schedules(ctx)
    sid = sched.list_schedules("atlas")[0]["id"]
    assert "Cancelled" in await cancel_schedule(ctx, sid)
    assert "No scheduled tasks" in await list_schedules(ctx)


# --- scheduler dispatch ---

async def test_scheduler_fires_agent(overlay):
    from src.core.scheduler import Scheduler

    class FakeMgr:
        agent_configs = {"atlas": {"routing": {"discord": {"account": "main"}}}}

        def __init__(self):
            self.calls = []

        async def handle_message(self, agent_id, message):
            self.calls.append((agent_id, message))

    sched.create_schedule("atlas", "chan9", "water the plants", "owner",
                          spec_type="every", spec="30", now=0.0)
    s = Scheduler(FakeMgr())
    due = sched.due_schedules(1000.0)
    assert len(due) == 1
    await s._fire(due[0])
    import asyncio
    await asyncio.sleep(0)
    agent_id, msg = s.agent_manager.calls[0]
    assert agent_id == "atlas"
    assert msg.channel_id == "chan9" and msg.bot_account == "main"
    assert "water the plants" in msg.content


def test_until_expires_and_disables(overlay):
    sched.create_schedule("a", "c", "do", "u", spec_type="every", spec="60",
                          until=1500.0, now=1000.0)
    assert sched.due_schedules(2000.0) == []                  # past 'until' → not fired
    assert sched.list_schedules("a")[0]["enabled"] is False   # auto-disabled


def test_until_within_window_fires(overlay):
    sched.create_schedule("a", "c", "do", "u", spec_type="every", spec="60",
                          until=5000.0, now=1000.0)
    assert len(sched.due_schedules(1061.0)) == 1              # within window, interval elapsed


def test_max_runs_disables_after_n(overlay):
    sched.create_schedule("a", "c", "do", "u", spec_type="every", spec="60",
                          max_runs=2, now=1000.0)
    assert len(sched.due_schedules(1061.0)) == 1              # run 1
    assert len(sched.due_schedules(1122.0)) == 1              # run 2 → hits max
    assert sched.due_schedules(1183.0) == []                  # disabled after max_runs
    s = sched.list_schedules("a")[0]
    assert s["enabled"] is False and s["run_count"] == 2


def test_set_fields_patches_record(overlay):
    r = sched.create_schedule("a", "c", "do", "u", spec_type="every", spec="60", now=1000.0)
    assert sched.set_fields(r["id"], announced=True, closed=False) is True
    s = sched.list_schedules("a")[0]
    assert s["announced"] is True and s["closed"] is False
    assert sched.set_fields("nope", x=1) is False   # unknown id → no-op
