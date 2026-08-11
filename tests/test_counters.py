"""Daily counters — routing/outcome metrics storage + bump sites."""

import pytest

from src.core.storage import Storage


@pytest.fixture
async def storage(tmp_path):
    s = Storage(db_path=str(tmp_path / "t.db"))
    await s.init()
    yield s
    await s.close()


async def test_counter_round_trip(storage):
    await storage.bump_counter("router.local")
    await storage.bump_counter("router.local")
    await storage.bump_counter("router.claude")
    c = await storage.get_counters(days=7)
    assert c == {"router.local": 2, "router.claude": 1}


async def test_counters_empty(storage):
    assert await storage.get_counters() == {}


async def test_bump_never_raises_without_table(tmp_path):
    s = Storage(db_path=str(tmp_path / "t2.db"))
    await s.init()
    await s._db.execute("DROP TABLE counters")
    await s.bump_counter("x")            # suppressed — must not raise
    await s.close()


async def test_router_decision_bumps(tmp_path, monkeypatch):
    """The manager bumps router.local/router.claude at the decision point."""
    from contextlib import asynccontextmanager

    from src.core.agent_manager import AgentManager
    from src.core.base import Connector, IncomingMessage, LLMProvider, LLMResponse
    from src.core.model_router import RouteDecision

    class RecordingProvider(LLMProvider):
        name = "recording"

        def __init__(self):
            super().__init__(config={})

        async def complete(self, messages, tools=None, stream=False, **kwargs):
            return LLMResponse(content="ok", stop_reason="end")

    class StubConnector(Connector):
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
    storage = Storage(db_path=str(tmp_path / "t.db"))
    await storage.init()
    local = RecordingProvider()
    mgr = AgentManager(
        agent_configs={"bot": {"project_dir": str(agent_dir),
                               "llm": {"provider": "mock"}, "tools": [],
                               "routing": {"stub": {"channels": []}}}},
        connectors={"stub": StubConnector()},
        llm_providers={"mock": RecordingProvider(), "local": local},
        memory_backends={}, storage=storage,
        defaults={"llm": {"router": {"enabled": True}}})

    async def fake_route(*a, **k):
        return RouteDecision("local", "simple @0.9")
    monkeypatch.setattr(mgr._model_router, "route", fake_route)

    await mgr.handle_message("bot", IncomingMessage(
        connector="stub", channel_id="c", user_id="u", user_name="d", content="hi"))
    import asyncio
    await asyncio.sleep(0.05)            # fire-and-forget bump tasks
    c = await storage.get_counters()
    assert c.get("router.local") == 1 and c.get("local.success") == 1
    await storage.close()
