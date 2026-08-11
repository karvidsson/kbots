"""When Claude auth is down, the manager alerts ops once (deduped)."""

from contextlib import asynccontextmanager

from src.core.agent_manager import AgentManager
from src.core.base import Connector, IncomingMessage, LLMProvider, LLMResponse
from src.core.router import Router


class AuthDownProvider(LLMProvider):
    name = "authdown"

    async def complete(self, messages, tools=None, stream=False, **kwargs):
        return LLMResponse(content="auth broken", stop_reason="auth_error",
                           session_id=None, model="x")


class RecordingAlerter:
    def __init__(self):
        self.messages = []

    def send_bg(self, message: str) -> None:
        self.messages.append(message)


class Stub(Connector):
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


def _manager(tmp_path, alerter):
    agent_dir = tmp_path / "agents" / "main"
    agent_dir.mkdir(parents=True)
    return AgentManager(
        agent_configs={
            "main": {
                "display_name": "MAIN",
                "project_dir": str(agent_dir),
                "llm": {"provider": "authdown"},
                "tools": [],
                "routing": {"stub": {"channels": []}},
            }
        },
        connectors={"stub": Stub()},
        llm_providers={"authdown": AuthDownProvider(config={})},
        memory_backends={},
        alerter=alerter,
    )


def _msg():
    return IncomingMessage(connector="stub", channel_id="c", user_id="u",
                           user_name="dev", content="hi")


async def test_auth_error_alerts_ops(tmp_path):
    alerter = RecordingAlerter()
    router = Router(_manager(tmp_path, alerter))
    await router.route(_msg())
    assert len(alerter.messages) == 1
    assert "Claude Code auth is down" in alerter.messages[0]
    assert "claude-auth" in alerter.messages[0]


async def test_auth_alert_is_deduped(tmp_path):
    alerter = RecordingAlerter()
    router = Router(_manager(tmp_path, alerter))
    await router.route(_msg())
    await router.route(_msg())  # within the 15-min window
    assert len(alerter.messages) == 1  # only alerted once
