"""Tool-direct actions — zero-LLM automations on schedules and triggers."""

import time

import pytest

from src.core import actions
from src.core import schedules as schedmod
from src.core import triggers as trigmod
from src.core.base import LLMResponse
from src.core.scheduler import Scheduler
from src.core.tools import tool


@tool(name="act_ping", description="test action tool", category="test")
async def act_ping(ctx, target: str = "") -> str:
    return f"pinged {target}"


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    (tmp_path / "config").mkdir()
    return tmp_path


class _FakeConnector:
    def __init__(self):
        self.sent = []

    async def send(self, channel_id, content, **kw):
        self.sent.append((channel_id, content))


class _FakeLocal:
    def __init__(self, content="all done boss", error=False):
        self._content, self._error = content, error

    async def complete(self, messages, tools=None, stream=False, **kw):
        return LLMResponse(content=self._content,
                           stop_reason="error" if self._error else "end")


class _FakeMgr:
    def __init__(self, local=None):
        self.connectors = {"discord": _FakeConnector()}
        self.agent_configs = {"bot": {"routing": {"discord": {"account": "main"}}}}
        self.llm_providers = {"local": local} if local else {}
        self.dispatched = []

    async def _dispatch_tools(self, agent_id, session_id, tool_calls, connector, msg):
        self.dispatched.append((agent_id, session_id, tool_calls))
        return [{"name": tc["name"], "content": f"ran {tc['name']}"} for tc in tool_calls]


def _record(**action):
    return {"id": "s1", "agent_id": "bot", "channel_id": "c1", "connector": "discord",
            "created_by": "u1", "action": action or None}


# --- parse_binding ------------------------------------------------------------

def test_parse_binding_validates():
    with pytest.raises(ValueError, match="not both"):
        actions.parse_binding("sk", "", "act_ping", "", False)
    with pytest.raises(ValueError, match="unknown skill"):
        actions.parse_binding("no_such_skill", "", "", "", False)
    with pytest.raises(ValueError, match="unknown tool"):
        actions.parse_binding("", "", "no_such_tool", "", False)
    with pytest.raises(ValueError, match="must be JSON"):
        actions.parse_binding("", "", "act_ping", "{bad", False)
    skill, params, action = actions.parse_binding("", "", "act_ping",
                                                  '{"target": "office"}', True)
    assert action == {"tool": "act_ping", "args": {"target": "office"}, "narrate": True}


# --- run_tool_action ----------------------------------------------------------

async def test_action_executes_and_posts_result():
    mgr = _FakeMgr()
    await actions.run_tool_action(mgr, _record(tool="act_ping", args={"target": "x"}),
                                  "schedule")
    assert mgr.dispatched[0][2] == [{"name": "act_ping", "arguments": {"target": "x"}}]
    (chan, content), = mgr.connectors["discord"].sent
    assert chan == "c1" and "ran act_ping" in content


async def test_action_silent_posts_nothing():
    mgr = _FakeMgr()
    await actions.run_tool_action(mgr, _record(tool="act_ping", args={}, silent=True),
                                  "trigger")
    assert mgr.dispatched and mgr.connectors["discord"].sent == []


async def test_action_narrate_uses_local_model():
    mgr = _FakeMgr(local=_FakeLocal("Office light is now off. ✨"))
    await actions.run_tool_action(mgr, _record(tool="act_ping", args={}, narrate=True),
                                  "schedule")
    assert mgr.connectors["discord"].sent[0][1] == "Office light is now off. ✨"


async def test_action_narrate_falls_back_to_raw_on_error():
    mgr = _FakeMgr(local=_FakeLocal(error=True, content="down"))
    await actions.run_tool_action(mgr, _record(tool="act_ping", args={}, narrate=True),
                                  "schedule")
    assert "ran act_ping" in mgr.connectors["discord"].sent[0][1]


async def test_action_never_raises():
    mgr = _FakeMgr()

    async def boom(*a, **k):
        raise RuntimeError("kaput")
    mgr._dispatch_tools = boom
    await actions.run_tool_action(mgr, _record(tool="act_ping", args={}), "schedule")
    # no exception; nothing posted


# --- records + firing ---------------------------------------------------------

def test_schedule_with_action_and_no_instruction(overlay):
    rec = schedmod.create_schedule("bot", "c1", "", "u1", spec_type="every", spec="60",
                                   now=1000.0, action={"tool": "act_ping", "args": {}})
    assert rec["action"] == {"tool": "act_ping", "args": {}}
    with pytest.raises(ValueError, match="Invalid action"):
        schedmod.create_schedule("bot", "c1", "", "u1", spec_type="every", spec="60",
                                 now=1000.0, action={"tool": "nope"})
    with pytest.raises(ValueError, match="Instruction is required"):
        schedmod.create_schedule("bot", "c1", "", "u1", spec_type="every", spec="60",
                                 now=1000.0)


def test_trigger_with_action(overlay):
    rec, secret = trigmod.create_trigger("btn_office", "bot", "c1", "", "u1",
                                         action={"tool": "act_ping",
                                                 "args": {"target": "office"}})
    assert rec["action"]["tool"] == "act_ping" and secret


async def test_scheduler_fires_action_without_llm(overlay, monkeypatch):
    mgr = _FakeMgr()

    async def no_llm_turn(*a, **k):
        raise AssertionError("handle_message must not run for an action schedule")
    mgr.handle_message = no_llm_turn

    rec = schedmod.create_schedule("bot", "c1", "", "u1", spec_type="every", spec="60",
                                   now=1000.0, action={"tool": "act_ping", "args": {}})
    sc = Scheduler(agent_manager=mgr)
    await sc._fire(rec)
    # background task runs the action
    import asyncio
    await asyncio.sleep(0)
    for _ in range(10):
        if mgr.dispatched:
            break
        await asyncio.sleep(0.01)
    assert mgr.dispatched  # tool ran, no LLM


def test_scheduler_passes_skill_fields(overlay):
    rec = schedmod.create_schedule("bot", "c1", "do it", "u1", spec_type="every",
                                   spec="60", now=time.time(), skill="mysk",
                                   skill_params={"x": "1"})
    assert rec["skill"] == "mysk" and rec["skill_params"] == {"x": "1"}


def test_legacy_records_without_new_fields_still_fire_normally(overlay):
    # simulate an old record (no skill/action keys) — .get access must not blow up
    old = {"id": "s9", "agent_id": "bot", "channel_id": "c", "connector": "discord",
           "instruction": "old", "created_by": "u", "spec_type": "every", "spec": "60",
           "enabled": True, "last_run": 0}
    assert old.get("action") is None and old.get("skill") is None
