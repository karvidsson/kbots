"""set_schedule_board tool (admin-gated) + live board-channel override."""

import pytest

from src.core import runtime_state
from src.core.base import ToolContext
from src.core.scheduler import Scheduler
from src.tools import schedule_admin


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.yaml").write_text("admin_users:\n  discord: ['999']\n")
    return tmp_path


async def test_set_board_requires_admin(overlay):
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="123")   # not an admin
    out = await schedule_admin.set_schedule_board(ctx, channel_id="555")
    assert "only an admin" in out.lower()
    assert runtime_state.get_flag("schedules_channel", None) is None       # unchanged


async def test_set_board_admin_enables_then_clears(overlay):
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="999")   # admin
    out = await schedule_admin.set_schedule_board(ctx, channel_id="555", bot="ops")
    assert "live" in out.lower() and "555" in out
    assert runtime_state.get_flag("schedules_channel") == "555"
    assert runtime_state.get_flag("schedules_bot") == "ops"

    out = await schedule_admin.set_schedule_board(ctx, channel_id="")       # clear override
    assert "off" in out.lower()
    # cleared, NOT masked with '' — config governs again
    assert runtime_state.get_flag("schedules_channel", None) is None
    assert runtime_state.get_flag("schedules_bot", None) is None


async def test_clear_reverts_to_config_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        "admin_users:\n  discord: ['999']\nschedules:\n  channel: '777'\n")
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="999")
    await schedule_admin.set_schedule_board(ctx, channel_id="555")          # override
    out = await schedule_admin.set_schedule_board(ctx, channel_id="")        # clear → config
    assert "777" in out and "config" in out.lower()
    assert runtime_state.get_flag("schedules_channel", None) is None         # override gone


def test_board_channel_override_wins_then_clears_to_config(overlay):
    sc = Scheduler(agent_manager=None, schedules_channel="from_config")
    assert sc._board_channel() == "from_config"          # no override → config default
    runtime_state.set_flag("schedules_channel", "live_chan")
    assert sc._board_channel() == "live_chan"             # runtime override wins
    runtime_state.clear_flag("schedules_channel")
    assert sc._board_channel() == "from_config"           # cleared → back to config (not masked)
