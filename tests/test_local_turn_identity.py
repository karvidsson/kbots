"""Tier-routed local turns must carry the agent's CLAUDE.md identity.

claude_code agents get no system prompt at build time (the CLI loads CLAUDE.md
itself). When the tier router swaps a turn to the local provider, the CLI isn't
running — without injection the model has no idea which agent it is and
improvises an identity from the message envelope.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

from src.core.agent_manager import AgentManager
from src.core.base import (
    Connector,
    IncomingMessage,
    LLMProvider,
    LLMResponse,
    MessageRole,
)


class StubConnector(Connector):
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


class RecordingProvider(LLMProvider):
    """Records the exact message list each complete() call receives."""
    name = "recording"

    def __init__(self):
        super().__init__(config={})
        self.calls: list[list] = []

    async def complete(self, messages, tools=None, stream=False, **kwargs):
        self.calls.append(list(messages))
        return LLMResponse(content="ok", stop_reason="end")


def _mk_manager(tmp_path, providers):
    agent_dir = tmp_path / "agents" / "bot"
    agent_dir.mkdir(parents=True)
    (agent_dir / "CLAUDE.md").write_text("You are **Pixel Fox** — the artist.")
    cfg = {"bot": {"project_dir": str(agent_dir),
                   "llm": {"provider": "claude_code"},
                   "tools": [], "routing": {"stub": {"channels": []}}}}
    return AgentManager(
        agent_configs=cfg, connectors={"stub": StubConnector()},
        llm_providers=providers, memory_backends={},
        defaults={"llm": {"router": {"enabled": True}}})


def _msg():
    return IncomingMessage(connector="stub", channel_id="c1", user_id="u",
                           user_name="dev", content="hi")


async def test_local_routed_turn_gets_identity_prompt(tmp_path, monkeypatch):
    cc, local = RecordingProvider(), RecordingProvider()
    mgr = _mk_manager(tmp_path, {"claude_code": cc, "local": local})

    async def force_local(*a, **k):
        return SimpleNamespace(target="local", reason="test")
    monkeypatch.setattr(mgr._model_router, "route", force_local)
    monkeypatch.setattr(mgr._model_router, "apply", lambda d, s: True)

    await mgr.handle_message("bot", _msg())

    assert local.calls and not cc.calls
    first = local.calls[0][0]
    assert first.role == MessageRole.SYSTEM
    assert "Pixel Fox" in first.content


async def test_claude_code_turn_gets_no_injected_prompt(tmp_path, monkeypatch):
    cc, local = RecordingProvider(), RecordingProvider()
    # The stub stands in for the real CLI provider, which reads the identity
    # file from the project dir itself (the engine keys off this attribute).
    cc.reads_project_context = True
    mgr = _mk_manager(tmp_path, {"claude_code": cc, "local": local})

    async def stay_claude(*a, **k):
        return SimpleNamespace(target="claude", reason="test")
    monkeypatch.setattr(mgr._model_router, "route", stay_claude)
    monkeypatch.setattr(mgr._model_router, "apply", lambda d, s: False)

    await mgr.handle_message("bot", _msg())

    assert cc.calls and not local.calls
    assert all(m.role != MessageRole.SYSTEM for m in cc.calls[0])
