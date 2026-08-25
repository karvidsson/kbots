"""Skill llm-pinning, restrict_tools, max_rounds, fallback — PR-1 behaviors."""

from contextlib import asynccontextmanager

import pytest

from src.core import skills as skills_mod
from src.core.agent_manager import AgentManager
from src.core.base import Connector, IncomingMessage, LLMProvider, LLMResponse, Skill
from src.core.tools import tool
from src.llm.mock import MockProvider

# --- fixtures ---------------------------------------------------------------

@tool(name="sp_alpha", description="test tool a", category="test")
async def sp_alpha(ctx) -> str:
    return "alpha-ran"


@tool(name="sp_beta", description="test tool b", category="test")
async def sp_beta(ctx) -> str:
    return "beta-ran"


@tool(name="sp_gamma", description="test tool c", category="test")
async def sp_gamma(ctx) -> str:
    return "gamma-ran"


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
    """Scriptable provider: records calls, plays back queued responses."""
    name = "recording"

    def __init__(self, responses=None):
        super().__init__(config={})
        self.calls: list[dict] = []
        self._responses = list(responses or [])

    async def complete(self, messages, tools=None, stream=False, **kwargs):
        self.calls.append({"tools": [t.name for t in (tools or [])],
                           "model": kwargs.get("model")})
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="recorded reply", stop_reason="end")


def _mk_manager(tmp_path, providers, agent_tools, defaults=None):
    agent_dir = tmp_path / "agents" / "bot"
    agent_dir.mkdir(parents=True)
    cfg = {"bot": {"project_dir": str(agent_dir), "llm": {"provider": "mock"},
                   "tools": agent_tools, "routing": {"stub": {"channels": []}}}}
    connector = StubConnector()
    mgr = AgentManager(agent_configs=cfg, connectors={"stub": connector},
                       llm_providers=providers, memory_backends={},
                       defaults=defaults or {})
    return mgr, connector


def _skill(monkeypatch, **kw):
    sk = Skill(name="task", description="", prompt="Do the thing: {x}", **kw)
    monkeypatch.setitem(skills_mod._skill_registry, "task", sk)
    return sk


def _msg(skill=None):
    return IncomingMessage(connector="stub", channel_id="c1", user_id="u",
                           user_name="dev", content="run it", skill=skill,
                           skill_params={"x": "y"} if skill else None)


# --- YAML parsing -----------------------------------------------------------

def test_skill_yaml_parses_new_keys(tmp_path):
    f = tmp_path / "s.yaml"
    f.write_text("name: s\nprompt: p\ntools: [sp_alpha]\nrestrict_tools: true\n"
                 "max_rounds: 4\nllm:\n  provider: local\n  model: qwen3.5:9b\n")
    sk = skills_mod._load_skill_file(f)
    assert sk.llm == {"provider": "local", "model": "qwen3.5:9b"}
    assert sk.restrict_tools is True and sk.max_rounds == 4


def test_skill_yaml_defaults_and_invalid_llm(tmp_path):
    f = tmp_path / "s.yaml"
    f.write_text("name: s\nprompt: p\n")
    sk = skills_mod._load_skill_file(f)
    assert sk.llm is None and sk.restrict_tools is False and sk.max_rounds == 0
    f.write_text("name: s\nprompt: p\nllm: local\n")   # not a mapping
    with pytest.raises(ValueError, match="must be a mapping"):
        skills_mod._load_skill_file(f)


# --- provider pin -----------------------------------------------------------

async def test_skill_pin_uses_pinned_provider_and_skips_router(tmp_path, monkeypatch):
    pinned = RecordingProvider()
    default = RecordingProvider()
    mgr, connector = _mk_manager(
        tmp_path, {"mock": default, "local": pinned}, ["sp_alpha"],
        defaults={"llm": {"router": {"enabled": True}}})

    async def router_should_not_run(*a, **k):
        raise AssertionError("router ran despite skill pin")
    monkeypatch.setattr(mgr._model_router, "route", router_should_not_run)

    _skill(monkeypatch, tools=["sp_alpha"], llm={"provider": "local", "model": "m9"})
    await mgr.handle_message("bot", _msg(skill="task"))
    assert len(pinned.calls) == 1 and pinned.calls[0]["model"] == "m9"
    assert default.calls == []
    assert connector.sent and connector.sent[0][1] == "recorded reply"


async def test_unknown_pin_provider_uses_default(tmp_path, monkeypatch):
    default = RecordingProvider()
    mgr, connector = _mk_manager(tmp_path, {"mock": default}, ["sp_alpha"])
    _skill(monkeypatch, llm={"provider": "nope"})
    await mgr.handle_message("bot", _msg(skill="task"))
    assert len(default.calls) == 1


# --- restrict_tools ---------------------------------------------------------

async def test_restrict_tools_intersects_with_agent_allowlist(tmp_path, monkeypatch):
    p = RecordingProvider()
    mgr, _ = _mk_manager(tmp_path, {"mock": p}, ["sp_alpha", "sp_beta"])
    # skill wants beta (allowed) + gamma (NOT in agent allowlist)
    _skill(monkeypatch, tools=["sp_beta", "sp_gamma"], restrict_tools=True)
    await mgr.handle_message("bot", _msg(skill="task"))
    assert p.calls[0]["tools"] == ["sp_beta"]           # intersection only


async def test_union_preserved_without_restrict(tmp_path, monkeypatch):
    p = RecordingProvider()
    mgr, _ = _mk_manager(tmp_path, {"mock": p}, ["sp_alpha"])
    _skill(monkeypatch, tools=["sp_beta"])               # no restrict → union
    await mgr.handle_message("bot", _msg(skill="task"))
    assert set(p.calls[0]["tools"]) == {"sp_alpha", "sp_beta"}


# --- fallback ---------------------------------------------------------------

async def test_pinned_error_falls_back_to_default(tmp_path, monkeypatch):
    pinned = RecordingProvider([LLMResponse(content="runtime down", stop_reason="error")])
    default = RecordingProvider([LLMResponse(content="claude saves the day", stop_reason="end")])
    mgr, connector = _mk_manager(tmp_path, {"mock": default, "local": pinned}, [])
    _skill(monkeypatch, llm={"provider": "local"})
    await mgr.handle_message("bot", _msg(skill="task"))
    assert len(pinned.calls) == 1 and len(default.calls) == 1
    assert connector.sent[0][1] == "claude saves the day"


async def test_fallback_false_sends_wrapped_error(tmp_path, monkeypatch):
    pinned = RecordingProvider([LLMResponse(content="runtime down", stop_reason="error")])
    default = RecordingProvider()
    mgr, connector = _mk_manager(tmp_path, {"mock": default, "local": pinned}, [])
    _skill(monkeypatch, llm={"provider": "local", "fallback": False})
    await mgr.handle_message("bot", _msg(skill="task"))
    assert default.calls == []
    assert connector.sent[0][1] == "⚠️ runtime down"     # visibly an error, not an answer


# --- max_rounds + cap notice ------------------------------------------------

async def test_max_rounds_caps_loop_and_sends_notice(tmp_path, monkeypatch):
    looping = RecordingProvider([
        LLMResponse(content="", stop_reason="end",
                    tool_calls=[{"name": "sp_alpha", "arguments": {}}]),
        LLMResponse(content="", stop_reason="end",
                    tool_calls=[{"name": "sp_alpha", "arguments": {}}]),
        LLMResponse(content="", stop_reason="end",
                    tool_calls=[{"name": "sp_alpha", "arguments": {}}]),
    ])
    mgr, connector = _mk_manager(tmp_path, {"mock": looping}, ["sp_alpha"])
    _skill(monkeypatch, tools=["sp_alpha"], max_rounds=2)
    await mgr.handle_message("bot", _msg(skill="task"))
    assert len(looping.calls) == 2                        # capped at skill.max_rounds
    assert "didn't converge" in connector.sent[0][1]      # no more silent failure


# --- session hygiene --------------------------------------------------------

async def test_pinned_turn_clears_stale_cli_session(tmp_path, monkeypatch):
    pinned = RecordingProvider()                          # returns no session_id
    mgr, _ = _mk_manager(tmp_path, {"mock": MockProvider(config={}), "local": pinned}, [])
    _skill(monkeypatch, llm={"provider": "local"})
    session = mgr._get_or_create_session("bot", "c1", "u")
    session.cli_session_id = "cli-stale"
    await mgr.handle_message("bot", _msg(skill="task"))
    assert session.cli_session_id is None                 # Claude rebuilds from history


async def test_restrict_tools_under_all_agent(tmp_path, monkeypatch):
    """tools:'all' agents: the skill list IS the scope (the 'all' string used to
    intersect to nothing — the create-then-operate loop's main case)."""
    p = RecordingProvider()
    mgr, _ = _mk_manager(tmp_path, {"mock": p}, "all")     # atlas-style agent
    _skill(monkeypatch, tools=["sp_alpha", "sp_beta"], restrict_tools=True)
    await mgr.handle_message("bot", _msg(skill="task"))
    assert set(p.calls[0]["tools"]) == {"sp_alpha", "sp_beta"}


async def test_create_skill_authors_local_pinned_yaml(tmp_path, monkeypatch):
    """The large-model → local-model handoff: create_skill(run_on_local=True)
    writes a skill the engine parses with llm pin + restrict + rounds."""
    from src.core import digest
    from src.core.base import ToolContext
    from src.tools.ingest import create_skill

    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(digest, "reload_skills", lambda: None)
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="u")
    out = await create_skill(
        ctx, name="ping_office", description="ping the office",
        prompt="Ping {target} and confirm in one line.",
        tools="sp_alpha,sp_beta", run_on_local=True,
        local_model="qwen3.5:9b", max_rounds=4)
    assert "LOCAL model" in out
    sk = skills_mod._load_skill_file(tmp_path / "skills" / "ping_office.yaml")
    assert sk.llm == {"provider": "local", "model": "qwen3.5:9b"}
    assert sk.restrict_tools is True and sk.max_rounds == 4
    assert sk.tools == ["sp_alpha", "sp_beta"]
    assert sk.parameters[0].name == "target"


async def test_create_skill_warns_on_long_local_prompt(tmp_path, monkeypatch):
    """Local operation degrades on long prompts — create_skill warns (non-blocking)
    when run_on_local and the prompt exceeds ~12 lines; silent otherwise."""
    from src.core import digest
    from src.core.base import ToolContext
    from src.tools.ingest import create_skill

    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(digest, "reload_skills", lambda: None)
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="u")
    long_prompt = "\n".join(f"Step {i}: do thing {i}." for i in range(14))
    out = await create_skill(
        ctx, name="long_local", description="d", prompt=long_prompt,
        tools="sp_alpha", run_on_local=True)
    assert "WARNING" in out and ">12 lines" in out
    out2 = await create_skill(
        ctx, name="long_claude", description="d", prompt=long_prompt,
        tools="sp_alpha", run_on_local=False)
    assert "WARNING" not in out2
