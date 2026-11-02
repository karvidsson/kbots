"""Per-agent MCP identity — regeneration keeps it, tools refuse the fallback.

Regression: regenerate_mcp_json() rebuilt every agent's .mcp.json without
KBOTS_AGENT_ID/KBOTS_PROJECT_DIR, collapsing all agents into the MCP server's
shared 'mcp-agent' fallback — which silently defeated per-agent tool scoping
(a private tool was recorded as owner 'mcp-agent').
"""

import json

from src.core.base import ToolContext
from src.tools.ingest import create_tool


def test_regenerate_mcp_json_keeps_identity(tmp_path, monkeypatch):
    from src.core.digest import regenerate_mcp_json

    overlay = tmp_path / "ov"
    (overlay / "config").mkdir(parents=True)
    (overlay / "config" / "mcp.yaml").write_text(
        "servers:\n  kbots-tools:\n    transport: stdio\n    command: py\n"
    )
    agent_dir = overlay / "agents" / "helper"
    agent_dir.mkdir(parents=True)
    (overlay / "config" / "agents.yaml").write_text(
        f"agents:\n  helper:\n    project_dir: {agent_dir}\n"
    )
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))

    updated = regenerate_mcp_json()

    assert agent_dir / ".mcp.json" in updated
    env = json.loads((agent_dir / ".mcp.json").read_text())["mcpServers"]["kbots-tools"]["env"]
    assert env["KBOTS_AGENT_ID"] == "helper"
    assert env["KBOTS_PROJECT_DIR"] == str(agent_dir)


def test_regenerate_mcp_json_keeps_the_agents_google_identity(tmp_path, monkeypatch):
    # Hand-set in .mcp.json, KBOTS_GOOGLE_ACCOUNT was dropped by the next
    # regeneration and the agent fell back to the install's default mailbox.
    from src.core.digest import regenerate_mcp_json

    overlay = tmp_path / "ov"
    (overlay / "config").mkdir(parents=True)
    (overlay / "config" / "mcp.yaml").write_text(
        "servers:\n  kbots-tools:\n    transport: stdio\n    command: py\n"
    )
    own, plain = overlay / "agents" / "owner", overlay / "agents" / "plain"
    own.mkdir(parents=True)
    plain.mkdir(parents=True)
    (overlay / "config" / "agents.yaml").write_text(
        f"agents:\n  owner:\n    project_dir: {own}\n    google_account: pixel-fox\n"
        f"  plain:\n    project_dir: {plain}\n"
    )
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))

    regenerate_mcp_json()

    env = lambda d: json.loads((d / ".mcp.json").read_text())["mcpServers"]["kbots-tools"]["env"]  # noqa: E731
    assert env(own)["KBOTS_GOOGLE_ACCOUNT"] == "pixel-fox"
    # No key means the install default: the variable is absent, never empty.
    assert "KBOTS_GOOGLE_ACCOUNT" not in env(plain)


async def test_create_tool_refuses_fallback_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))

    out = await create_tool(
        ToolContext(agent_id="mcp-agent"), "zz_fallback_probe", "d",
        "from src.core.tools import tool\n"
        "@tool(name='zz_fallback_probe', description='d')\n"
        "async def zz_fallback_probe(ctx) -> str:\n    return 'x'\n",
    )

    assert out.startswith("ERROR")
    assert "mcp-agent" in out
    assert not (tmp_path / "tools" / "zz_fallback_probe.py").exists()
