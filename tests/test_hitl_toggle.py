"""Tests for the HITL runtime enable/disable toggle.

The toggle is backed by shared overlay runtime state (src/core/runtime_state),
so it works across the engine and the MCP-tool process.
"""

import aiosqlite
import pytest

from src.core.hitl import HITLGate


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


@pytest.fixture
async def gate(tmp_path, overlay):
    db = await aiosqlite.connect(tmp_path / "t.db")
    g = HITLGate({"channel": "c", "gated_tools": ["send_email"]}, db)
    await g.init_schema()
    yield g
    await db.close()


async def test_default_enabled(gate):
    await gate.load_enabled()
    assert gate.enabled is True


async def test_config_default_off(tmp_path, overlay):
    db = await aiosqlite.connect(tmp_path / "t.db")
    g = HITLGate({"channel": "c", "enabled": False}, db)
    await g.init_schema()
    await g.load_enabled()  # no flag set yet → falls back to config default
    assert g.enabled is False
    await db.close()


async def test_set_enabled_persists_across_processes(tmp_path, overlay):
    db = await aiosqlite.connect(tmp_path / "t.db")
    g = HITLGate({"channel": "c"}, db)
    await g.init_schema()
    await g.set_enabled(False)
    assert g.enabled is False
    await db.close()

    # A fresh gate (simulating another process) reads the shared flag,
    # overriding its config default.
    db2 = await aiosqlite.connect(tmp_path / "t.db")
    g2 = HITLGate({"channel": "c", "enabled": True}, db2)
    await g2.init_schema()
    await g2.load_enabled()
    assert g2.enabled is False
    await db2.close()


async def test_toggle_back_on(gate):
    await gate.set_enabled(False)
    await gate.set_enabled(True)
    await gate.load_enabled()
    assert gate.enabled is True


async def test_set_hitl_tool_admin_only(tmp_path, overlay, monkeypatch):
    """The set_hitl tool flips the same shared flag, but admin-only."""
    from src.core import runtime_state
    from src.core.base import ToolContext
    from src.tools.hitl_admin import set_hitl

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        'admin_users:\n  discord: ["111"]\n')

    # non-admin can't flip it
    out = await set_hitl(ToolContext(agent_id="a", user_id="999"), enabled=False)
    assert out.startswith("ERROR")
    assert runtime_state.get_flag("hitl_enabled") is None

    # admin can
    out = await set_hitl(ToolContext(agent_id="a", user_id="111"), enabled=False)
    assert "OFF" in out
    assert runtime_state.get_flag("hitl_enabled") is False


# --- Runtime channel override (set_hitl_channel) ---

async def test_channel_override_wins_over_config(tmp_path, overlay):
    from src.core import runtime_state
    db = await aiosqlite.connect(tmp_path / "t.db")
    g = HITLGate({"channel": "config-ch"}, db)
    assert g.channel_id == "config-ch"
    runtime_state.set_flag("hitl_channel", "999")
    assert g.channel_id == "999"          # live — no re-init needed
    runtime_state.clear_flag("hitl_channel")
    assert g.channel_id == "config-ch"    # cleared → config again
    await db.close()


async def test_channel_override_fills_empty_config(tmp_path, overlay):
    from src.core import runtime_state
    db = await aiosqlite.connect(tmp_path / "t.db")
    g = HITLGate({"channel": ""}, db)
    assert not g.channel_id
    runtime_state.set_flag("hitl_channel", "999")
    assert g.channel_id == "999"
    await db.close()


async def test_set_hitl_channel_tool(overlay, monkeypatch):
    from src.core import runtime_state
    from src.core.base import ToolContext
    from src.tools import hitl_admin

    monkeypatch.setattr(hitl_admin, "_is_admin", lambda uid: uid == "111")
    ctx_admin = ToolContext(agent_id="main", user_id="111")
    ctx_other = ToolContext(agent_id="main", user_id="222")

    out = await hitl_admin.set_hitl_channel(ctx_other, "999")
    assert out.startswith("ERROR") and not runtime_state.get_flag("hitl_channel")

    out = await hitl_admin.set_hitl_channel(ctx_admin, "not-a-number")
    assert out.startswith("ERROR")

    out = await hitl_admin.set_hitl_channel(ctx_admin, "999")
    assert "✅" in out and runtime_state.get_flag("hitl_channel") == "999"

    out = await hitl_admin.set_hitl_channel(ctx_admin, "")
    assert not runtime_state.get_flag("hitl_channel")
