"""End-to-end message-path test: connector → router → agent manager → mock LLM → reply.

Wires the real Router and AgentManager with a stub connector and the mock
provider — no main.py boot, no preflight, no Discord, no Claude quota.
"""

from contextlib import asynccontextmanager

import pytest

from src.core.agent_manager import AgentManager
from src.core.base import Connector, IncomingMessage
from src.core.router import Router
from src.llm.mock import MockProvider


class StubConnector(Connector):
    """Records everything sent through it."""
    name = "stub"

    def __init__(self):
        super().__init__(config={})
        self.sent: list[tuple[str, str]] = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, channel_id: str, content: str, **kwargs):
        self.sent.append((channel_id, content))

    @asynccontextmanager
    async def typing(self, channel_id: str, **kwargs):
        yield


@pytest.fixture
def pipeline(tmp_path):
    """Router + AgentManager + mock LLM + stub connector, minimally wired."""
    agent_dir = tmp_path / "agents" / "testbot"
    agent_dir.mkdir(parents=True)

    agent_configs = {
        "testbot": {
            "display_name": "TESTBOT",
            "project_dir": str(agent_dir),
            "llm": {"provider": "mock"},
            "tools": [],
            "routing": {"stub": {"channels": []}},
        }
    }
    connector = StubConnector()
    manager = AgentManager(
        agent_configs=agent_configs,
        connectors={"stub": connector},
        llm_providers={"mock": MockProvider(config={})},
        memory_backends={},
    )
    router = Router(manager)
    return router, connector


def _msg(content: str, channel: str = "chan-1") -> IncomingMessage:
    return IncomingMessage(
        connector="stub", channel_id=channel,
        user_id="dev-user", user_name="dev", content=content,
    )


async def test_message_flows_end_to_end(pipeline):
    router, connector = pipeline
    await router.route(_msg("hello pipeline"))

    assert len(connector.sent) == 1
    channel_id, content = connector.sent[0]
    assert channel_id == "chan-1"
    assert "hello pipeline" in content
    assert content.startswith("[mock] echo:")


async def test_unrouted_connector_gets_no_reply(pipeline):
    router, connector = pipeline
    msg = _msg("hello")
    msg.connector = "discord"  # testbot only routes 'stub'
    await router.route(msg)
    assert connector.sent == []


async def test_channel_filter(tmp_path):
    agent_dir = tmp_path / "agents" / "scoped"
    agent_dir.mkdir(parents=True)
    connector = StubConnector()
    manager = AgentManager(
        agent_configs={
            "scoped": {
                "display_name": "SCOPED",
                "project_dir": str(agent_dir),
                "llm": {"provider": "mock"},
                "tools": [],
                "routing": {"stub": {"channels": ["allowed-chan"]}},
            }
        },
        connectors={"stub": connector},
        llm_providers={"mock": MockProvider(config={})},
        memory_backends={},
    )
    router = Router(manager)

    await router.route(_msg("nope", channel="other-chan"))
    assert connector.sent == []

    await router.route(_msg("yes", channel="allowed-chan"))
    assert len(connector.sent) == 1
    assert connector.sent[0][0] == "allowed-chan"


async def test_session_continuity(pipeline):
    """Second message in the same channel reuses the session (mock session id saved)."""
    router, connector = pipeline
    await router.route(_msg("first"))
    await router.route(_msg("second"))
    assert len(connector.sent) == 2

    manager = router.agent_manager
    session = manager.sessions["testbot:chan-1"]
    assert session.message_count == 2
    assert session.cli_session_id  # saved from mock response


async def test_canned_responses():
    provider = MockProvider(config={"responses": ["one", "two"]})
    from src.core.base import Message, MessageRole
    r1 = await provider.complete([Message(role=MessageRole.USER, content="x")])
    r2 = await provider.complete([Message(role=MessageRole.USER, content="x")])
    r3 = await provider.complete([Message(role=MessageRole.USER, content="x")])
    assert [r1.content, r2.content, r3.content] == ["one", "two", "one"]
