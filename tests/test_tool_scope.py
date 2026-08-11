"""Tests for tool scoping — private agent-created tools + promotion to global."""

import yaml

from src.core.base import IncomingMessage, ToolContext
from src.core.tool_scope import (
    get_entry,
    hidden_tools_for_agent,
    load_scope,
    promote_to_global,
    record_tool,
)
from src.tools.ingest import create_tool, promote_tool
from tests.test_create_tool import VALID_SOURCE


def _ctx(agent_id="helper"):
    return ToolContext(agent_id=agent_id)


# --- scope registry ---

def test_scope_lifecycle(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))

    record_tool("fx_rates", owner="helper")
    assert get_entry("fx_rates") == {"owner": "helper", "global": False}

    # Owner sees it; others don't
    assert "fx_rates" not in hidden_tools_for_agent("helper")
    assert "fx_rates" in hidden_tools_for_agent("main")

    assert promote_to_global("fx_rates") is True
    assert get_entry("fx_rates")["global"] is True
    assert "fx_rates" not in hidden_tools_for_agent("main")

    assert promote_to_global("unknown_tool") is False


def test_scope_empty_without_overlay(monkeypatch):
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    assert load_scope() == {}
    assert hidden_tools_for_agent("anyone") == []


# --- create_tool records private ownership ---

async def test_created_tool_is_private(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    result = await create_tool(
        _ctx("helper"), "zz_dyn_priv", "Private test tool",
        VALID_SOURCE.replace("zz_dyn_echo", "zz_dyn_priv"),
    )
    assert "PRIVATE to you (helper)" in result
    assert get_entry("zz_dyn_priv") == {"owner": "helper", "global": False}
    assert "zz_dyn_priv" in hidden_tools_for_agent("other")


# --- promote_tool permissions ---

def _write_agents(overlay):
    agents = {
        "agents": {
            "main": {"display_name": "MAIN", "tier": "coordinator"},
            "helper": {"display_name": "Helper", "tier": "assistant"},
            "bystander": {"display_name": "Bystander", "tier": "assistant"},
        }
    }
    (overlay / "config" / "agents.yaml").write_text(yaml.dump(agents))


async def test_owner_can_promote(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    _write_agents(overlay)
    record_tool("mytool", owner="helper")

    result = await promote_tool(_ctx("helper"), "mytool")
    assert "now GLOBAL" in result
    assert get_entry("mytool")["global"] is True


async def test_other_assistant_cannot_promote(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    _write_agents(overlay)
    record_tool("mytool", owner="helper")

    result = await promote_tool(_ctx("bystander"), "mytool")
    assert result.startswith("ERROR")
    assert get_entry("mytool")["global"] is False


async def test_coordinator_can_promote(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    _write_agents(overlay)
    record_tool("mytool", owner="helper")

    result = await promote_tool(_ctx("main"), "mytool")
    assert "now GLOBAL" in result


async def test_promote_unknown_and_builtin(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    _write_agents(overlay)

    assert "ERROR" in await promote_tool(_ctx("main"), "does_not_exist")
    # A core tool has no scope entry — already global by nature
    result = await promote_tool(_ctx("main"), "web_search")
    assert "already global by nature" in result


# --- runtime enforcement: other agents' LLM calls get the tool blocked ---

async def test_private_tool_hidden_from_other_agents_llm(overlay, tmp_path, monkeypatch):
    from src.core.agent_manager import AgentManager
    from src.core.router import Router
    from src.llm.mock import MockProvider

    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    record_tool("secret_tool", owner="owner_agent")

    captured: dict = {}

    class RecordingMock(MockProvider):
        async def complete(self, messages, tools=None, stream=False, **kwargs):
            captured.update(kwargs)
            return await super().complete(messages, tools=tools, stream=stream, **kwargs)

    from contextlib import asynccontextmanager

    from src.core.base import Connector

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
            self.sent.append((channel_id, content))

        @asynccontextmanager
        async def typing(self, channel_id, **kwargs):
            yield

    agent_dir = tmp_path / "agents" / "other_agent"
    agent_dir.mkdir(parents=True)
    connector = Stub()
    manager = AgentManager(
        agent_configs={
            "other_agent": {
                "display_name": "OTHER",
                "project_dir": str(agent_dir),
                "llm": {"provider": "mock"},
                "tools": [],
                "routing": {"stub": {"channels": []}},
            }
        },
        connectors={"stub": connector},
        llm_providers={"mock": RecordingMock(config={})},
        memory_backends={},
    )
    router = Router(manager)
    await router.route(IncomingMessage(
        connector="stub", channel_id="c", user_id="u", user_name="dev", content="hi",
    ))

    assert connector.sent, "agent should still reply"
    assert "mcp__kbots-tools__secret_tool" in (captured.get("disallowed_tools") or [])


def test_cli_grants_explicit_for_all_tools(monkeypatch, tmp_path):
    """tools:'all' must pass an explicit --allowedTools list (regression:
    wildcard/accumulated grants broke post-rename → every MCP call denied).
    Hermetic: a deployed overlay's mcp.yaml adds external server grants,
    so point the resolver at an empty one."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "mcp.yaml").write_text("servers: {}\n")
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    from src.core.agent_manager import compute_cli_tool_grants
    allowed, disallowed = compute_cli_tool_grants("all")
    assert allowed, "tools:'all' must not produce an empty/None allow list"
    assert disallowed is None
    assert all(t.startswith("mcp__kbots-tools__") for t in allowed)
    assert "mcp__kbots-tools__memory_search" in allowed


def test_cli_grants_restricted_list(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "mcp.yaml").write_text("servers: {}\n")
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    from src.core.agent_manager import compute_cli_tool_grants
    allowed, disallowed = compute_cli_tool_grants(["memory_search"])
    assert allowed == ["mcp__kbots-tools__memory_search"]
    assert "mcp__kbots-tools__memory_search" not in disallowed
    assert len(disallowed) > 0


def test_cli_grants_include_external_mcp_servers(tmp_path, monkeypatch):
    """External MCP servers (mcp.yaml) need server-level allows — they are
    not in the kbots registry (regression: agent self-granted via shell)."""
    import yaml as _yaml

    overlay = tmp_path / "ov"
    (overlay / "config").mkdir(parents=True)
    _yaml.dump({"servers": {"kbots-tools": {"transport": "stdio", "command": "x"},
                            "hostinger-vps": {"transport": "stdio", "command": "npx"}}},
               open(overlay / "config" / "mcp.yaml", "w"))
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    from src.core.agent_manager import compute_cli_tool_grants
    allowed, _ = compute_cli_tool_grants("all")
    assert "mcp__hostinger-vps" in allowed
    assert "mcp__kbots-tools" not in allowed  # per-tool entries cover the registry
    allowed_r, disallowed_r = compute_cli_tool_grants(["memory_search"])
    assert "mcp__hostinger-vps" in allowed_r
    assert "mcp__hostinger-vps" not in (disallowed_r or [])


async def test_run_command_blocks_permission_file_edits():
    import platform as _platform

    from src.core.base import ToolContext
    from src.tools.computer import run_command
    if _platform.system() != "Darwin":
        return
    ctx = ToolContext(agent_id="t")
    out = await run_command(ctx, "python3 -c 'x' >> agents/milo/.claude/settings.json")
    assert out.startswith("Blocked:")
    out2 = await run_command(ctx, "cat config/secrets.enc")
    assert out2.startswith("Blocked:")
