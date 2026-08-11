"""Same-channel messages serialize (no concurrent --resume collision);
different channels still run in parallel."""

import asyncio
from contextlib import asynccontextmanager

from src.core.agent_manager import AgentManager
from src.core.base import Connector, IncomingMessage, LLMProvider, LLMResponse
from src.core.router import Router


class _OverlapProbe(LLMProvider):
    name = "mock"

    def __init__(self, config):
        super().__init__(config)
        self.active = 0
        self.max_active = 0

    async def complete(self, messages, tools=None, stream=False, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.05)
        self.active -= 1
        return LLMResponse(content="ok", session_id="s")


class _Stub(Connector):
    name = "stub"

    def __init__(self):
        super().__init__(config={})
        self.sent = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, channel_id, content, **kwargs):
        self.sent.append(content)

    @asynccontextmanager
    async def typing(self, channel_id, **kwargs):
        yield


def _router(tmp_path, provider):
    (tmp_path / "agents" / "main").mkdir(parents=True)
    mgr = AgentManager(
        agent_configs={"main": {
            "display_name": "MAIN", "project_dir": str(tmp_path / "agents" / "main"),
            "llm": {"provider": "mock"}, "tools": [],
            "routing": {"stub": {"channels": []}},
        }},
        connectors={"stub": _Stub()},
        llm_providers={"mock": provider},
        memory_backends={},
    )
    return Router(mgr)


def _msg(channel):
    return IncomingMessage(connector="stub", channel_id=channel,
                           user_id="u", user_name="dev", content="hi")


async def test_same_channel_serialized(tmp_path):
    provider = _OverlapProbe(config={})
    router = _router(tmp_path, provider)
    # Two messages to the SAME channel, fired concurrently
    await asyncio.gather(router.route(_msg("c1")), router.route(_msg("c1")))
    assert provider.max_active == 1  # never overlapped


async def test_different_channels_parallel(tmp_path):
    provider = _OverlapProbe(config={})
    router = _router(tmp_path, provider)
    await asyncio.gather(router.route(_msg("c1")), router.route(_msg("c2")))
    assert provider.max_active == 2  # ran in parallel


async def test_queued_followup_is_acknowledged(tmp_path):
    provider = _OverlapProbe(config={})
    router = _router(tmp_path, provider)
    connector = router.agent_manager.connectors["stub"]
    # Two to the same channel: the second arrives while the first holds the lock
    await asyncio.gather(router.route(_msg("c1")), router.route(_msg("c1")))
    acks = [s for s in connector.sent if "Noted" in s]
    assert len(acks) == 1  # exactly one queue acknowledgment
    # both turns still produced their normal reply
    assert connector.sent.count("ok") == 2
