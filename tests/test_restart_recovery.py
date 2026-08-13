"""Restart recovery — turns killed at the drain timeout resume next boot."""

import asyncio
import json

from src.core.recovery import (
    FILENAME,
    RECOVERY_PROMPT,
    build_recovery_message,
    load_and_clear,
    save_interrupted,
)


def _turn(agent="jarvis", channel="123", connector="discord", **kw):
    return {"agent_id": agent, "channel_id": channel, "connector": connector,
            "user_id": "42", "bot_account": "main", **kw}


def test_save_load_roundtrip_and_clear(tmp_path):
    assert save_interrupted(tmp_path, [_turn()]) == 1
    loaded = load_and_clear(tmp_path)
    assert len(loaded) == 1 and loaded[0]["agent_id"] == "jarvis"
    # File is consumed — a second boot must not re-deliver
    assert not (tmp_path / FILENAME).exists()
    assert load_and_clear(tmp_path) == []


def test_save_skips_internal_and_dedupes(tmp_path):
    turns = [
        _turn(channel="internal:jarvis:engineer"),   # loopback — no real channel
        _turn(channel="555"),
        _turn(channel="555"),                        # same conversation, one nudge
        _turn(agent="engineer", channel="555"),      # different agent, kept
        {"agent_id": "", "channel_id": "9"},         # malformed — dropped
    ]
    assert save_interrupted(tmp_path, turns) == 2
    loaded = load_and_clear(tmp_path)
    assert {(t["agent_id"], t["channel_id"]) for t in loaded} == {
        ("jarvis", "555"), ("engineer", "555")}


def test_nothing_saved_writes_no_file(tmp_path):
    assert save_interrupted(tmp_path, [_turn(channel="internal:x:y")]) == 0
    assert not (tmp_path / FILENAME).exists()


def test_corrupt_file_is_cleared_not_fatal(tmp_path):
    (tmp_path / FILENAME).write_text("{broken")
    assert load_and_clear(tmp_path) == []
    assert not (tmp_path / FILENAME).exists()


def test_recovery_message_shape():
    msg = build_recovery_message(_turn())
    assert msg.connector == "discord"
    assert msg.channel_id == "123"
    assert msg.bot_account == "main"
    assert msg.user_name == "restart-recovery"
    assert msg.content == RECOVERY_PROMPT
    assert "NO_REPLY" in msg.content


async def test_inflight_snapshot_tracks_running_turns(tmp_path):
    from contextlib import asynccontextmanager

    from src.core.agent_manager import AgentManager
    from src.core.base import Connector, IncomingMessage, LLMProvider, LLMResponse

    release = asyncio.Event()
    entered = asyncio.Event()

    class SlowProvider(LLMProvider):
        name = "slow"

        def __init__(self):
            super().__init__(config={})

        async def complete(self, messages, tools=None, stream=False, **kwargs):
            entered.set()
            await release.wait()
            return LLMResponse(content="done", stop_reason="end")

    class NullConnector(Connector):
        name = "stub"

        def __init__(self):
            super().__init__(config={})

        async def start(self):
            pass

        async def stop(self):
            pass

        async def send(self, channel_id, content, **kwargs):
            pass

        @asynccontextmanager
        async def typing(self, channel_id, **kwargs):
            yield

    agent_dir = tmp_path / "agents" / "bot"
    agent_dir.mkdir(parents=True)
    mgr = AgentManager(
        agent_configs={"bot": {"project_dir": str(agent_dir),
                               "llm": {"provider": "slow"}, "tools": [],
                               "routing": {"stub": {"channels": []}}}},
        connectors={"stub": NullConnector()},
        llm_providers={"slow": SlowProvider()},
        memory_backends={})

    msg = IncomingMessage(connector="stub", channel_id="c9", user_id="u1",
                          user_name="dev", content="do a long thing",
                          bot_account="main")
    task = asyncio.create_task(mgr.handle_message("bot", msg))
    await asyncio.wait_for(entered.wait(), timeout=5)

    snap = mgr.inflight_snapshot()
    assert len(snap) == 1
    assert snap[0]["agent_id"] == "bot"
    assert snap[0]["channel_id"] == "c9"
    assert snap[0]["connector"] == "stub"
    # The snapshot is exactly what the shutdown path persists
    assert save_interrupted(tmp_path, snap) == 1
    saved = json.loads((tmp_path / FILENAME).read_text())
    assert saved["turns"][0]["agent_id"] == "bot"

    release.set()
    await asyncio.wait_for(task, timeout=5)
    assert mgr.inflight_snapshot() == []
