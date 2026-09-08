"""A provider override applies on the next turn, with no restart.

Every provider the deployment knows is built at boot and kept in a dict, so
switching an agent between them is a lookup. What used to force a restart was
that the agent's provider name was read from agents.yaml once into memory and
the override table only carried model and effort. These tests pin the three
things a live switch has to get right:

- the next turn runs on the overridden provider, and any caller that asks
  for the agent's provider (reflector, summariser) gets the same answer;
- the model sent with it is the new provider's default unless a model override
  says otherwise, because model names are vendor-local;
- a CLI session id minted by the old provider is dropped rather than handed to
  a backend that has never seen it.
"""

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from src.core.agent_manager import AgentManager
from src.core.base import Connector, IncomingMessage, LLMProvider, LLMResponse
from src.core.storage import Storage


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
    """Records what it was called with; answers with a fixed session id."""
    name = "recording"

    def __init__(self, session_id=None):
        super().__init__(config={})
        self.calls: list[dict] = []
        self._session_id = session_id

    async def complete(self, messages, tools=None, stream=False, **kwargs):
        self.calls.append({"model": kwargs.get("model"),
                           "session_id": kwargs.get("session_id")})
        return LLMResponse(content="ok", stop_reason="end",
                           session_id=self._session_id)


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as tmp:
        s = Storage(db_path=str(Path(tmp) / "test.db"))
        await s.init()
        try:
            yield s
        finally:
            await s.close()


def _mk_manager(tmp_path, storage, providers):
    agent_dir = tmp_path / "agents" / "bot"
    agent_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"bot": {"project_dir": str(agent_dir),
                   "llm": {"provider": "alpha", "model": "alpha-large"},
                   "tools": [], "routing": {"stub": {"channels": []}}}}
    connector = StubConnector()
    mgr = AgentManager(agent_configs=cfg, connectors={"stub": connector},
                       llm_providers=providers, memory_backends={},
                       defaults={}, storage=storage)
    return mgr


def _msg(text="hello"):
    return IncomingMessage(connector="stub", channel_id="c1", user_id="u",
                           user_name="dev", content=text)


# --- storage: the provider is recorded with the session id -----------------

@pytest.mark.asyncio
async def test_session_provider_is_saved_and_restored(storage):
    await storage.get_or_create_session("bot:c1", "bot", "c1", "u")
    await storage.save_cli_session_id("bot:c1", "sess-1", provider="alpha")
    row = await storage.get_or_create_session("bot:c1", "bot", "c1", "u")
    assert row["cli_session_id"] == "sess-1"
    assert row["cli_session_provider"] == "alpha"

    # clearing the id clears the provider with it
    await storage.save_cli_session_id("bot:c1", "")
    row = await storage.get_or_create_session("bot:c1", "bot", "c1", "u")
    assert row["cli_session_id"] == ""
    assert row["cli_session_provider"] is None


@pytest.mark.asyncio
async def test_session_provider_column_is_added_to_an_old_database(tmp_path):
    """A database from before the column must open, and read as 'unknown'."""
    import aiosqlite
    db = tmp_path / "old.db"
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, "
            "channel_id TEXT, user_id TEXT, cli_session_id TEXT, created_at REAL, "
            "last_active REAL, summary TEXT)")
        await conn.execute(
            "INSERT INTO sessions (id, agent_id, cli_session_id) VALUES ('bot:c1', 'bot', 'old-1')")
        await conn.commit()
    s = Storage(db_path=str(db))
    await s.init()
    try:
        row = await s.get_or_create_session("bot:c1", "bot", "c1", "u")
        assert row["cli_session_id"] == "old-1"
        assert row["cli_session_provider"] is None
    finally:
        await s.close()


# --- engine: the override wins, on this turn --------------------------------

@pytest.mark.asyncio
async def test_provider_override_applies_on_the_next_turn(tmp_path, storage):
    alpha, beta = RecordingProvider(), RecordingProvider()
    mgr = _mk_manager(tmp_path, storage, {"alpha": alpha, "beta": beta})

    await mgr.handle_message("bot", _msg())
    assert len(alpha.calls) == 1 and beta.calls == []
    assert alpha.calls[0]["model"] == "alpha-large"

    await storage.set_agent_override("bot", "provider", "beta")
    await mgr.handle_message("bot", _msg())
    assert len(alpha.calls) == 1 and len(beta.calls) == 1
    # agents.yaml's model belongs to alpha; beta gets its own default
    assert beta.calls[0]["model"] == ""
    # and the synchronous lookup the reflector uses agrees
    assert mgr._get_agent_llm("bot") is beta

    await storage.set_agent_override("bot", "model", "beta-mini")
    await mgr.handle_message("bot", _msg())
    assert beta.calls[1]["model"] == "beta-mini"

    # clearing the override puts the agent back on agents.yaml
    await storage.set_agent_override("bot", "provider", None)
    await storage.set_agent_override("bot", "model", None)
    await mgr.handle_message("bot", _msg())
    assert len(alpha.calls) == 2 and alpha.calls[1]["model"] == "alpha-large"
    assert mgr._get_agent_llm("bot") is alpha


@pytest.mark.asyncio
async def test_unknown_provider_override_is_ignored_not_fatal(tmp_path, storage):
    alpha = RecordingProvider()
    mgr = _mk_manager(tmp_path, storage, {"alpha": alpha})
    await storage.set_agent_override("bot", "provider", "nope")
    await mgr.handle_message("bot", _msg())
    assert len(alpha.calls) == 1
    assert mgr._get_agent_llm("bot") is alpha


@pytest.mark.asyncio
async def test_boot_preload_primes_the_cache_before_any_turn(tmp_path, storage):
    alpha, beta = RecordingProvider(), RecordingProvider()
    await storage.set_agent_override("bot", "provider", "beta")
    mgr = _mk_manager(tmp_path, storage, {"alpha": alpha, "beta": beta})
    assert mgr._get_agent_llm("bot") is alpha       # nothing loaded yet
    await mgr.load_provider_overrides()
    assert mgr._get_agent_llm("bot") is beta        # what the reflector sees


# --- engine: a foreign CLI session is dropped, not resumed ------------------

@pytest.mark.asyncio
async def test_switching_provider_drops_the_old_cli_session(tmp_path, storage):
    alpha = RecordingProvider(session_id="alpha-sess")
    beta = RecordingProvider(session_id="beta-sess")
    mgr = _mk_manager(tmp_path, storage, {"alpha": alpha, "beta": beta})

    await mgr.handle_message("bot", _msg())
    await mgr.handle_message("bot", _msg())
    assert alpha.calls[1]["session_id"] == "alpha-sess"   # resume works within alpha
    row = await storage.get_or_create_session("bot:c1", "bot", "c1", "u")
    assert (row["cli_session_id"], row["cli_session_provider"]) == ("alpha-sess", "alpha")

    await storage.set_agent_override("bot", "provider", "beta")
    await mgr.handle_message("bot", _msg())
    assert beta.calls[0]["session_id"] is None            # alpha's id was not handed over
    row = await storage.get_or_create_session("bot:c1", "bot", "c1", "u")
    assert (row["cli_session_id"], row["cli_session_provider"]) == ("beta-sess", "beta")

    await mgr.handle_message("bot", _msg())
    assert beta.calls[1]["session_id"] == "beta-sess"     # and beta resumes its own


@pytest.mark.asyncio
async def test_session_with_no_recorded_provider_is_left_alone(tmp_path, storage):
    """Rows from before the column carry no provider; do not throw their id away."""
    alpha = RecordingProvider(session_id="alpha-sess")
    mgr = _mk_manager(tmp_path, storage, {"alpha": alpha})
    await storage.get_or_create_session("bot:c1", "bot", "c1", "u")
    await storage.save_cli_session_id("bot:c1", "legacy-1")   # no provider
    await mgr.handle_message("bot", _msg())
    assert alpha.calls[0]["session_id"] == "legacy-1"


# --- background jobs pick a model the current provider accepts --------------

@pytest.mark.asyncio
async def test_background_model_follows_the_provider_override(tmp_path, storage):
    alpha, beta = RecordingProvider(), RecordingProvider()
    mgr = _mk_manager(tmp_path, storage, {"alpha": alpha, "beta": beta})
    alpha.name, beta.name = "alpha", "beta"

    # configured: alpha-large on alpha
    assert await mgr.background_model_for("bot", alpha) == "alpha-large"
    # a cheap model configured for this provider wins
    assert await mgr.background_model_for("bot", alpha, cheap={"alpha": "alpha-mini"}) == "alpha-mini"
    # switched to beta with no model: beta's own default, never alpha-large
    await storage.set_agent_override("bot", "provider", "beta")
    assert await mgr.background_model_for("bot", beta) is None
    # switched with a model: that model
    await storage.set_agent_override("bot", "model", "beta-pro")
    assert await mgr.background_model_for("bot", beta) == "beta-pro"
    # and the cheap map only applies to its own provider
    assert await mgr.background_model_for("bot", beta, cheap={"alpha": "alpha-mini"}) == "beta-pro"
