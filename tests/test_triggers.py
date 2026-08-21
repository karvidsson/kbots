"""Event triggers: store CRUD, per-registration secrets, killswitch, dispatch."""

import json

import pytest

from src.core import triggers as trig
from src.core.base import ToolContext
from src.tools.triggers_admin import create_trigger, delete_trigger, list_triggers


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    (tmp_path / "config").mkdir()
    return tmp_path


# --- store + per-registration secrets ---

def test_create_returns_unique_secret_stored_hashed(overlay):
    r1, s1 = trig.create_trigger("event_a", "atlas", "c", "do a", "u")
    r2, s2 = trig.create_trigger("event_b", "atlas", "c", "do b", "u")
    assert s1 != s2 and len(s1) > 20
    # only the hash is stored, never the plaintext
    raw = (overlay / "data" / "triggers.json").read_text()
    assert s1 not in raw and s2 not in raw
    assert "secret_hash" in raw
    # verify checks the right secret, constant-time
    assert trig.verify_secret(r1, s1) is True
    assert trig.verify_secret(r1, s2) is False  # a different trigger's secret can't fire it
    assert trig.verify_secret(r1, "guess") is False


def test_store_crud(overlay):
    r, _ = trig.create_trigger("Kitchen Light Off", "atlas", "chan1", "hallway on", "user1")
    assert r["event"] == "kitchen_light_off" and r["id"] == "t1"
    assert trig.get_by_id("t1") == r
    assert trig.list_triggers("atlas") == [r]
    assert trig.list_triggers("other") == []
    assert trig.delete_trigger("t1") is True
    assert trig.get_by_id("t1") is None
    assert trig.delete_trigger("t1") is False


def test_validates(overlay):
    with pytest.raises(ValueError, match="Invalid event"):
        trig.create_trigger("bad name!", "a", "c", "do", "u")
    with pytest.raises(ValueError, match="Instruction is required"):
        trig.create_trigger("ok_event", "a", "c", "  ", "u")


# --- killswitch ---

def test_killswitch(overlay):
    assert trig.is_enabled() is True
    trig.set_enabled(False)
    assert trig.is_enabled() is False
    trig.set_enabled(True)
    assert trig.is_enabled() is True


def test_migrates_old_bare_list(overlay):
    (overlay / "triggers.json").write_text(json.dumps(
        [{"id": "t1", "event": "e", "agent_id": "a", "enabled": True}]))
    assert trig.is_enabled() is True  # migrated → default enabled
    assert trig.get_by_id("t1")["event"] == "e"


# --- tools ---

async def test_create_tool_returns_unique_url_and_secret(overlay):
    ctx = ToolContext(agent_id="atlas", channel_id="chan1", user_id="u1")
    out = await create_trigger(ctx, "kitchen light off", "turn on hallway light")
    assert "/event/t1" in out                 # per-trigger URL
    assert "X-Webhook-Secret:" in out          # secret shown once
    assert "OWN secret" in out
    assert trig.get_by_id("t1")["agent_id"] == "atlas"


async def test_list_and_delete_tools(overlay):
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="u")
    await create_trigger(ctx, "ev1", "do one")
    assert "on **ev1**" in await list_triggers(ctx)
    assert "deleted" in await delete_trigger(ctx, "t1")
    assert "No triggers" in await list_triggers(ctx)


# --- webhook dispatch: per-trigger auth + killswitch ---

class _FakeMgr:
    agent_configs = {"atlas": {"routing": {"discord": {"account": "main"}}}}

    def __init__(self):
        self.calls = []

    async def handle_message(self, agent_id, message):
        self.calls.append((agent_id, message))


async def _conn(overlay):
    from src.connectors.webhook import WebhookConnector
    conn = WebhookConnector(config={})
    conn._agent_manager = _FakeMgr()
    return conn


async def test_fire_invokes_agent_with_data(overlay):
    trig.create_trigger("door_opened", "atlas", "chan9", "greet them", "owner1")
    conn = await _conn(overlay)
    ok = await conn._fire(trig.get_by_id("t1"), {"who": "Kris"})
    assert ok is True
    import asyncio
    await asyncio.sleep(0)
    agent_id, msg = conn._agent_manager.calls[0]
    assert agent_id == "atlas"
    assert msg.connector == "discord" and msg.channel_id == "chan9"
    assert msg.bot_account == "main"
    assert "greet them" in msg.content and "Kris" in msg.content


async def test_killswitch_blocks_fire(overlay):
    trig.create_trigger("ev", "atlas", "c", "do", "u")
    conn = await _conn(overlay)
    trig.set_enabled(False)
    ok = await conn._fire(trig.get_by_id("t1"), {})
    assert ok is False
    assert conn._agent_manager.calls == []  # no agent invoked
