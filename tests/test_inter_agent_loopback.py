"""Loopback internal API — inter-agent tools work from the MCP subprocess.

ask_agent/send_to_agent run inside the MCP server, a subprocess with no
handle on the AgentManager. The InternalAPI (main process, loopback-only,
per-boot bearer token) carries their calls across the process boundary; the
depth guard is enforced on both sides.
"""

import aiohttp
import pytest

from src.core.base import ToolContext
from src.core.internal_api import InternalAPI
from src.tools.builtin import MAX_INTER_AGENT_DEPTH, ask_agent, send_to_agent


class FakeMgr:
    def __init__(self):
        self.agent_configs = {"alpha": {}, "beta": {}}
        self.calls = []

    async def handle_internal_message(self, agent_id, message):
        self.calls.append((agent_id, message.content, message._inter_agent_depth))
        return f"pong from {agent_id}"

    async def deliver_inter_agent_message(self, target, from_agent, content, depth):
        self.calls.append((target, content, depth))
        return {"delivery": "channel", "channel_id": "999"}


@pytest.fixture
async def api():
    mgr = FakeMgr()
    a = InternalAPI(mgr)  # ephemeral port
    await a.start()
    yield a, mgr
    await a.stop()


async def _post(a: InternalAPI, payload: dict, token: str | None = None):
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"http://127.0.0.1:{a.port}/agent-message", json=payload,
            headers={"Authorization": f"Bearer {token or a.token}"},
        ) as r:
            return r.status, await r.json(content_type=None)


# --- server ---

async def test_ask_roundtrip_and_depth_increment(api):
    a, mgr = api
    status, data = await _post(a, {"target": "beta", "content": "hi",
                                   "from_agent": "alpha", "depth": 0, "wait": True})
    assert status == 200
    assert data["response"] == "pong from beta"
    assert mgr.calls == [("beta", "hi", 1)]  # depth incremented for the callee


async def test_send_delivers_visibly(api):
    a, mgr = api
    status, data = await _post(a, {"target": "beta", "content": "fyi",
                                   "from_agent": "alpha", "wait": False})
    assert status == 200 and data["status"] == "dispatched"
    assert data["delivery"] == "channel" and data["channel_id"] == "999"
    assert mgr.calls == [("beta", "fyi", 1)]  # depth incremented for the callee


async def test_rejects_bad_token(api):
    a, _ = api
    status, data = await _post(a, {"target": "beta", "content": "x",
                                   "from_agent": "alpha"}, token="wrong")
    assert status == 401 and data["error"] == "unauthorized"


async def test_rejects_unknown_target_with_roster(api):
    a, _ = api
    status, data = await _post(a, {"target": "nobody", "content": "x",
                                   "from_agent": "alpha"})
    assert status == 404
    assert data["available"] == ["alpha", "beta"]


async def test_rejects_depth_limit_and_self_call(api):
    a, mgr = api
    status, _ = await _post(a, {"target": "beta", "content": "x", "from_agent": "alpha",
                                "depth": MAX_INTER_AGENT_DEPTH})
    assert status == 429
    status, _ = await _post(a, {"target": "alpha", "content": "x", "from_agent": "alpha"})
    assert status == 400
    assert mgr.calls == []


# --- tool fallback (no ctx.agent_manager, env points at the loopback) ---

def _env(monkeypatch, a: InternalAPI, depth: int | None = None):
    for k, v in a.env.items():
        monkeypatch.setenv(k, v)
    if depth is not None:
        monkeypatch.setenv("KBOTS_INTER_AGENT_DEPTH", str(depth))


async def test_ask_agent_tool_uses_loopback(api, monkeypatch):
    a, mgr = api
    _env(monkeypatch, a)
    out = await ask_agent(ToolContext(agent_id="alpha"), "beta", "question?")
    assert out == "pong from beta"
    assert mgr.calls == [("beta", "question?", 1)]


async def test_send_to_agent_tool_uses_loopback(api, monkeypatch):
    a, mgr = api
    _env(monkeypatch, a)
    out = await send_to_agent(ToolContext(agent_id="alpha"), "beta", "note")
    # The result must be honest about where the reply goes — not "dispatched".
    assert "<#999>" in out and "NOT returned to you" in out
    assert mgr.calls == [("beta", "note", 1)]


async def test_tool_reports_unknown_agent(api, monkeypatch):
    a, _ = api
    _env(monkeypatch, a)
    out = await ask_agent(ToolContext(agent_id="alpha"), "nobody", "q")
    assert "Unknown agent 'nobody'" in out and "beta" in out


async def test_tool_depth_guard_from_env(api, monkeypatch):
    a, mgr = api
    _env(monkeypatch, a, depth=MAX_INTER_AGENT_DEPTH)
    out = await ask_agent(ToolContext(agent_id="alpha"), "beta", "q")
    assert "depth limit" in out
    assert mgr.calls == []


async def test_tool_without_env_keeps_legacy_error(monkeypatch):
    monkeypatch.delenv("KBOTS_INTERNAL_API", raising=False)
    monkeypatch.delenv("KBOTS_INTERNAL_TOKEN", raising=False)
    out = await ask_agent(ToolContext(agent_id="alpha"), "beta", "q")
    assert out == "No agent manager available — inter-agent communication not supported."


# --- depth threading into the CLI subprocess env ---

def test_build_env_threads_depth(monkeypatch):
    from src.llm.claude_code import ClaudeCodeProvider

    p = ClaudeCodeProvider({})
    env = p._build_env(inter_agent_depth=2)
    assert env["KBOTS_INTER_AGENT_DEPTH"] == "2"
    assert "KBOTS_INTER_AGENT_DEPTH" not in p._build_env()  # absent at depth 0
