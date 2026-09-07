"""Fire-and-forget inter-agent sends deliver visibly.

A send used to run the target's turn in a hidden internal side-session and
discard the response — the target's channel self genuinely couldn't find the
message afterwards, and the outcome reached no one. Sends now route into the
target's home channel: the incoming message is posted there, the turn runs
with the channel session's full context, and the reply lands where the owner
can see it. The internal side-session remains only as a fallback for targets
with no resolvable home channel (and for ask_agent, which returns its answer
to the caller).
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from src.core.agent_manager import AgentManager
from src.core.base import Connector, IncomingMessage, LLMProvider, LLMResponse


class _EchoLLM(LLMProvider):
    name = "mock"

    def __init__(self, config):
        super().__init__(config)
        self.prompts = []

    async def complete(self, messages, tools=None, stream=False, **kwargs):
        self.prompts.append([m.content for m in messages])
        return LLMResponse(content="on it", session_id="s")


class _Stub(Connector):
    name = "stub"

    def __init__(self):
        super().__init__(config={})
        self.sent = []  # (channel_id, content, bot_account)

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, channel_id, content, **kwargs):
        self.sent.append((channel_id, content, kwargs.get("bot_account")))

    @asynccontextmanager
    async def typing(self, channel_id, **kwargs):
        yield


def _mgr(tmp_path, agent_cfg_extra=None):
    (tmp_path / "agents" / "beta").mkdir(parents=True)
    cfg = {
        "display_name": "Beta",
        "project_dir": str(tmp_path / "agents" / "beta"),
        "llm": {"provider": "mock"},
        "tools": [],
        "routing": {"stub": {"account": "beta-bot", "channels": []}},
    }
    cfg.update(agent_cfg_extra or {})
    stub = _Stub()
    llm = _EchoLLM({})
    mgr = AgentManager(
        agent_configs={"beta": cfg},
        connectors={"stub": stub},
        llm_providers={"mock": llm},
        memory_backends={},
    )
    return mgr, stub, llm


async def _drain(mgr):
    tasks = [t for t in asyncio.all_tasks()
             if t.get_name().startswith("inter-agent:")]
    if tasks:
        await asyncio.wait(tasks, timeout=5)


# --- home channel resolution ---

async def test_home_channel_explicit_config(tmp_path):
    mgr, _, _ = _mgr(tmp_path, {"routing": {"stub": {
        "account": "beta-bot", "home_channel": "42", "channels": ["77"]}}})
    assert await mgr._resolve_home_channel("beta") == ("stub", "42", "beta-bot")


async def test_home_channel_first_routed_channel(tmp_path):
    mgr, _, _ = _mgr(tmp_path, {"routing": {"stub": {
        "account": "beta-bot", "channels": ["77", "88"]}}})
    assert await mgr._resolve_home_channel("beta") == ("stub", "77", "beta-bot")


async def test_home_channel_falls_back_to_latest_session(tmp_path):
    mgr, _, _ = _mgr(tmp_path)  # wildcard routing
    mgr._get_or_create_session("beta", "internal:alpha:beta", "alpha")
    mgr._get_or_create_session("beta", "555", "user1")
    assert await mgr._resolve_home_channel("beta") == ("stub", "555", "beta-bot")


async def test_home_channel_none_when_unresolvable(tmp_path):
    mgr, _, _ = _mgr(tmp_path)  # wildcard routing, no sessions, no storage
    assert await mgr._resolve_home_channel("beta") is None


# --- delivery ---

async def test_send_delivers_into_home_channel(tmp_path):
    mgr, stub, llm = _mgr(tmp_path, {"routing": {"stub": {
        "account": "beta-bot", "channels": ["555"]}}})

    result = await mgr.deliver_inter_agent_message("beta", "alpha", "do the thing", 1)
    assert result == {"delivery": "channel", "channel_id": "555"}
    await _drain(mgr)

    # The incoming message is posted to the channel (visible record) and the
    # reply follows it — nothing appears out of nowhere, nothing is hidden.
    assert stub.sent[0] == ("555", "📨 **alpha → beta:** do the thing", "beta-bot")
    assert stub.sent[-1][0] == "555" and "on it" in stub.sent[-1][1]

    # The turn ran in the CHANNEL session with inter-agent context injected.
    assert mgr._session_key("beta", "555") in mgr.sessions
    prompt = "\n".join(llm.prompts[0])
    assert '<inter-agent-message from="alpha">' in prompt
    assert "do the thing" in prompt


async def test_send_falls_back_to_internal_session(tmp_path):
    mgr, stub, llm = _mgr(tmp_path)  # wildcard routing, no session to inherit

    result = await mgr.deliver_inter_agent_message("beta", "alpha", "psst", 1)
    assert result == {"delivery": "internal", "channel_id": None}
    await _drain(mgr)

    assert llm.prompts and "psst" in "\n".join(llm.prompts[0])
    assert stub.sent == []  # internal turns post nothing


async def test_channel_delivery_threads_depth(tmp_path):
    mgr, _, _ = _mgr(tmp_path, {"routing": {"stub": {
        "account": "beta-bot", "channels": ["555"]}}})
    seen = {}

    async def spy(agent_id, message):
        seen["depth"] = message._inter_agent_depth
        seen["sender"] = message._inter_agent_sender

    mgr.handle_message = spy
    await mgr.deliver_inter_agent_message("beta", "alpha", "hi", 2)
    await _drain(mgr)
    assert seen == {"depth": 2, "sender": "alpha"}


async def test_concurrent_asks_serialize_on_internal_session(tmp_path):
    """Two asks to the same target must not run concurrently — they share one
    CLI session and a concurrent --resume collides."""
    mgr, _, _ = _mgr(tmp_path)
    active = {"now": 0, "max": 0}
    orig = mgr._handle_internal_message_inner

    async def probe(*args, **kwargs):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        try:
            await asyncio.sleep(0.05)
            return await orig(*args, **kwargs)
        finally:
            active["now"] -= 1

    mgr._handle_internal_message_inner = probe
    msg = IncomingMessage(connector="internal", channel_id="internal:alpha:beta",
                          user_id="alpha", user_name="agent:alpha", content="q")
    await asyncio.gather(
        mgr.handle_internal_message("beta", msg),
        mgr.handle_internal_message("beta", msg),
    )
    assert active["max"] == 1


# --- a goal's room is not a home channel, live or retired ---

async def test_home_channel_skips_a_goal_room(tmp_path, monkeypatch):
    """"Most recently active" is a trailing indicator, so a goal's room is the
    freshest thing on record for exactly the agents who worked hardest in it.
    Twice a later message to one of them landed in a dead room, and once in a
    LIVE goal's room, where it arrived carrying that goal's context in front of
    its participants and read as work on it. Neither is a home.
    """
    from src.core import goals as store

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "goals.db"))
    monkeypatch.setattr(store, "_db", None)
    store._cache.clear()
    try:
        goal = store.create_goal("Launch", "d", "beta", "goal-chan", "u")
        store.update_goal(goal["id"], "beta", status="brainstorm")

        mgr, _, _ = _mgr(tmp_path)  # wildcard routing, no home_channel
        mgr._get_or_create_session("beta", "old-home", "user1")
        mgr._get_or_create_session("beta", "goal-chan", "user1")  # newest

        # live goal: unrelated mail still does not belong in its room
        assert await mgr._resolve_home_channel("beta") == ("stub", "old-home", "beta-bot")

        store.update_goal(goal["id"], "beta", status="abandoned")
        store._cache.clear()
        assert await mgr._resolve_home_channel("beta") == ("stub", "old-home", "beta-bot")
    finally:
        if store._db is not None:
            store._db.close()
        store._db = None
        store._cache.clear()


async def test_no_home_when_every_channel_belongs_to_a_goal(tmp_path, monkeypatch, caplog):
    """Dropping the message is the safe outcome, but it must not be silent:
    without a log line it is indistinguishable from an agent nobody has ever
    spoken to."""
    from src.core import goals as store

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "goals.db"))
    monkeypatch.setattr(store, "_db", None)
    store._cache.clear()
    try:
        goal = store.create_goal("Launch", "d", "beta", "goal-chan", "u")
        store.update_goal(goal["id"], "beta", status="brainstorm")

        mgr, _, _ = _mgr(tmp_path)
        mgr._get_or_create_session("beta", "goal-chan", "user1")

        with caplog.at_level(logging.WARNING):
            assert await mgr._resolve_home_channel("beta") is None
        assert "No home channel for 'beta'" in caplog.text
    finally:
        if store._db is not None:
            store._db.close()
        store._db = None
        store._cache.clear()


async def test_home_channel_ignores_goals_for_ordinary_channels(tmp_path, monkeypatch):
    """A channel no goal has ever owned must not be touched by this check."""
    from src.core import goals as store

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "goals.db"))
    monkeypatch.setattr(store, "_db", None)
    store._cache.clear()
    try:
        mgr, _, _ = _mgr(tmp_path)
        mgr._get_or_create_session("beta", "555", "user1")
        assert await mgr._resolve_home_channel("beta") == ("stub", "555", "beta-bot")
    finally:
        if store._db is not None:
            store._db.close()
        store._db = None
        store._cache.clear()
# --- an ask is written down ---

async def test_ask_persists_both_sides_of_the_turn(tmp_path):
    """An ask_agent turn runs a full LLM + tool loop and hands real work back
    to the caller. It used to leave no record at all, so the answering agent
    could not recall, quote or check what it had said — and on 2026-09-06 that
    cost a teammate a false accusation, because the reviewer looked for its own
    review, found nothing, and told the owner the endorsement was not its own.
    """
    from src.core.storage import Storage

    mgr, _, _ = _mgr(tmp_path)
    storage = Storage(db_path=tmp_path / "kbots.db")
    await storage.init()
    mgr.storage = storage
    try:
        msg = IncomingMessage(
            connector="internal", channel_id="internal:alpha:beta",
            user_id="alpha", user_name="agent:alpha",
            content="review this plan please")
        answer = await mgr.handle_internal_message("beta", msg)
        assert answer == "on it"

        # load_history returns newest first
        rows = await storage.load_history("beta:internal:alpha:beta")
        assert [(m.role.value, m.content) for m in reversed(rows)] == [
            ("user", "review this plan please"), ("assistant", "on it")]
    finally:
        await storage.close()


async def test_ask_without_storage_still_answers(tmp_path):
    """Persistence is a record, not a dependency: no storage must not break
    inter-agent asks."""
    mgr, _, _ = _mgr(tmp_path)
    assert mgr.storage is None
    msg = IncomingMessage(
        connector="internal", channel_id="internal:alpha:beta",
        user_id="alpha", user_name="agent:alpha", content="ping")
    assert await mgr.handle_internal_message("beta", msg) == "on it"
