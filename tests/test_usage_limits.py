"""Usage-limit handling: model downgrade helpers, session stickiness, alerts, token query."""

from contextlib import asynccontextmanager

from src.core.agent_manager import AgentManager
from src.core.base import Connector, IncomingMessage, LLMProvider, LLMResponse
from src.core.router import Router
from src.core.storage import Storage
from src.llm.claude_code import _DOWNGRADE_NEXT, _extract_reset_hint, _model_family

# --- provider helpers ---

def test_model_family():
    assert _model_family("opus") == "opus"
    assert _model_family("claude-sonnet-4-5-20250929") == "sonnet"
    assert _model_family("haiku") == "haiku"
    assert _model_family("gpt-4") is None


def test_downgrade_chain():
    assert _DOWNGRADE_NEXT["opus"] == "sonnet"
    assert _DOWNGRADE_NEXT["sonnet"] == "haiku"
    assert "haiku" not in _DOWNGRADE_NEXT  # cheapest — nowhere to fall


def test_reset_hint_extraction():
    assert "reset" in _extract_reset_hint("You've hit your limit. Resets at 3:00 PM today.").lower()
    assert _extract_reset_hint("generic error, no limit info") is None


# --- session stickiness + alerts via a mock provider ---

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


class DowngradeProvider(LLMProvider):
    """First call reports a usage downgrade to sonnet; records the model asked for."""
    name = "mock"

    def __init__(self, config):
        super().__init__(config)
        self.models_seen = []

    async def complete(self, messages, tools=None, stream=False, **kwargs):
        self.models_seen.append(kwargs.get("model"))
        if len(self.models_seen) == 1:
            return LLMResponse(content="ok on sonnet", model="sonnet",
                               usage_downgraded=True, reset_hint="resets at 3pm",
                               session_id="s1")
        return LLMResponse(content="ok", model="sonnet", session_id="s1")


class HardLimitProvider(LLMProvider):
    name = "mock"

    async def complete(self, messages, tools=None, stream=False, **kwargs):
        return LLMResponse(content="capped", stop_reason="usage_limit",
                           reset_hint="resets tomorrow")


def _manager(tmp_path, provider, alerter):
    agent_dir = tmp_path / "agents" / "main"
    agent_dir.mkdir(parents=True)
    return AgentManager(
        agent_configs={
            "main": {
                "display_name": "MAIN",
                "project_dir": str(agent_dir),
                "llm": {"provider": "mock", "model": "opus"},
                "tools": [],
                "routing": {"stub": {"channels": []}},
            }
        },
        connectors={"stub": Stub()},
        llm_providers={"mock": provider},
        memory_backends={},
        alerter=alerter,
    )


def _msg():
    return IncomingMessage(connector="stub", channel_id="c", user_id="u",
                           user_name="dev", content="hi")


async def test_downgrade_sticks_and_alerts(tmp_path):
    provider = DowngradeProvider(config={})
    alerter = RecordingAlerter()
    mgr = _manager(tmp_path, provider, alerter)
    router = Router(mgr)

    await router.route(_msg())   # opus requested, provider downgrades to sonnet
    await router.route(_msg())   # session should now request sonnet directly

    assert provider.models_seen[0] == "opus"
    assert provider.models_seen[1] == "sonnet"  # stuck on the cheaper model
    session = mgr.sessions["main:c"]
    assert session.model_override == "sonnet"
    assert session.model_override_until > 0
    assert len(alerter.messages) == 1
    assert "switched" in alerter.messages[0].lower()
    assert "sonnet" in alerter.messages[0]


async def test_hard_limit_alerts(tmp_path):
    alerter = RecordingAlerter()
    router = Router(_manager(tmp_path, HardLimitProvider(config={}), alerter))
    await router.route(_msg())
    assert len(alerter.messages) == 1
    assert "usage limit reached" in alerter.messages[0].lower()


async def test_downgrade_reverts_after_window(tmp_path):
    provider = DowngradeProvider(config={})
    mgr = _manager(tmp_path, provider, RecordingAlerter())
    router = Router(mgr)
    await router.route(_msg())
    session = mgr.sessions["main:c"]
    session.model_override_until = 1  # force the window to have elapsed
    await router.route(_msg())
    assert provider.models_seen[1] == "opus"  # reverted to configured model
    assert session.model_override is None


# --- token usage query ---

async def test_get_token_usage(tmp_path):
    storage = Storage(db_path=tmp_path / "s.db")
    await storage.init()
    db = storage._db
    await db.execute("INSERT INTO sessions (id, agent_id) VALUES ('s1', 'main')")
    await db.execute("INSERT INTO sessions (id, agent_id) VALUES ('s2', 'helper')")
    for sid, toks in [("s1", 100), ("s1", 50), ("s2", 30)]:
        await db.execute(
            "INSERT INTO messages (session_id, role, content, tokens_used) "
            "VALUES (?, 'assistant', 'x', ?)",
            (sid, toks),
        )
    await db.commit()

    usage = await storage.get_token_usage(days=1)
    by_agent = {r["agent_id"]: r for r in usage}
    assert by_agent["main"]["tokens"] == 150
    assert by_agent["main"]["messages"] == 2
    assert by_agent["helper"]["tokens"] == 30
    assert usage[0]["agent_id"] == "main"  # sorted by tokens desc
    await storage.close()
