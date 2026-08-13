"""Tests for src/core/agent_scaffold.py — shared by setup wizard and create_agent tool."""

import json

import pytest
import yaml

from src.core.agent_scaffold import cc_allow_for_tier, scaffold_agent


@pytest.fixture
def overlay(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "agents").mkdir()
    return tmp_path


def test_scaffold_creates_all_files(overlay, tmp_path):
    engine = tmp_path / "engine"
    written = scaffold_agent(
        overlay, "research", "Research Bot", "Finds things out",
        model="opus", tier="assistant", personality="curious",
        engine_root=engine,
    )

    agents_yaml = overlay / "config" / "agents.yaml"
    agent_dir = overlay / "agents" / "research"
    assert agents_yaml in written
    assert (agent_dir / "AGENTS.md") in written
    assert (agent_dir / "CLAUDE.md") in written  # thin stub importing AGENTS.md
    assert (agent_dir / ".mcp.json") in written
    assert (agent_dir / ".claude" / "settings.json") in written

    cfg = yaml.safe_load(agents_yaml.read_text())
    entry = cfg["agents"]["research"]
    assert entry["display_name"] == "Research Bot"
    assert entry["llm"] == {"provider": "claude_code", "model": "opus"}
    # Non-privileged agents lose only the shell; file writes stay (folder-scoped)
    assert entry["disallow_builtins"] == ["Bash"]
    assert "privileged" not in entry
    assert entry["routing"]["discord"]["account"] == "main"

    claude_md = (agent_dir / "AGENTS.md").read_text()
    assert "Research Bot" in claude_md
    assert "Be curious" in claude_md
    assert "## How to work" in claude_md
    assert "## Codify Repetitive Work" in claude_md
    assert "Prefer code over prompt" in claude_md

    mcp = json.loads((agent_dir / ".mcp.json").read_text())
    server = mcp["mcpServers"]["kbots-tools"]
    assert server["cwd"] == str(engine.resolve())
    assert server["env"]["KBOTS_OVERLAY"] == str(overlay)
    # Bot identity must be pinned — without these the MCP server falls back to
    # the first configured account and Discord tools act as another bot.
    assert server["env"]["KBOTS_AGENT_ID"] == "research"
    assert server["env"]["KBOTS_BOT_ACCOUNT"] == "main"

    settings = json.loads((agent_dir / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["allow"] == cc_allow_for_tier("assistant")


def test_tier_isolation_model(overlay):
    """Assistants are folder-confined; coordinator reads everywhere; tier persisted."""
    assert "Read(./**)" in cc_allow_for_tier("assistant")
    assert "Read(*)" not in cc_allow_for_tier("assistant")
    assert "Read(*)" in cc_allow_for_tier("coordinator")
    assert "Bash(*)" in cc_allow_for_tier("privileged")
    # Every tier has full file ops in its own working folder
    assert "Write(./**)" in cc_allow_for_tier("assistant")
    assert "Edit(./**)" in cc_allow_for_tier("assistant")
    assert "Write(./**)" in cc_allow_for_tier("coordinator")
    assert "Write(*)" in cc_allow_for_tier("privileged")
    # ...but confined tiers still can't write OUTSIDE their folder
    assert "Write(*)" not in cc_allow_for_tier("assistant")

    scaffold_agent(overlay, "worker", "Worker", "Scoped agent", tier="assistant")
    cfg = yaml.safe_load((overlay / "config" / "agents.yaml").read_text())
    assert cfg["agents"]["worker"]["tier"] == "assistant"
    settings = json.loads(
        (overlay / "agents" / "worker" / ".claude" / "settings.json").read_text()
    )
    assert "Read(./**)" in settings["permissions"]["allow"]


def test_full_control_main_agent_is_privileged(overlay):
    """A full-control main agent scaffolds as privileged: Bash(*), no builtin blocks."""
    scaffold_agent(overlay, "main", "MAIN", "Full control agent", tier="privileged")
    cfg = yaml.safe_load((overlay / "config" / "agents.yaml").read_text())
    entry = cfg["agents"]["main"]
    assert entry["privileged"] is True
    assert "disallow_builtins" not in entry
    settings = json.loads(
        (overlay / "agents" / "main" / ".claude" / "settings.json").read_text()
    )
    assert "Bash(*)" in settings["permissions"]["allow"]


def test_privileged_tier(overlay):
    scaffold_agent(overlay, "ops", "Ops", "Ops agent", tier="privileged")
    cfg = yaml.safe_load((overlay / "config" / "agents.yaml").read_text())
    entry = cfg["agents"]["ops"]
    assert entry["privileged"] is True
    assert "disallow_builtins" not in entry
    settings = json.loads((overlay / "agents" / "ops" / ".claude" / "settings.json").read_text())
    assert "Bash(*)" in settings["permissions"]["allow"]


def test_refuses_duplicate(overlay):
    scaffold_agent(overlay, "main", "Main", "Primary")
    with pytest.raises(ValueError, match="already exists"):
        scaffold_agent(overlay, "main", "Main 2", "Clone")


def test_exist_ok_updates_yaml_but_keeps_files(overlay):
    scaffold_agent(overlay, "main", "Main", "Primary", model="sonnet")
    claude_md = overlay / "agents" / "main" / "AGENTS.md"
    original = claude_md.read_text()

    scaffold_agent(overlay, "main", "Main v2", "Primary", model="opus", exist_ok=True)
    cfg = yaml.safe_load((overlay / "config" / "agents.yaml").read_text())
    assert cfg["agents"]["main"]["display_name"] == "Main v2"
    assert cfg["agents"]["main"]["llm"]["model"] == "opus"
    # Existing files are never overwritten
    assert claude_md.read_text() == original


def test_appends_to_existing_agents_yaml(overlay):
    scaffold_agent(overlay, "main", "Main", "Primary")
    scaffold_agent(overlay, "helper", "Helper", "Second agent")
    cfg = yaml.safe_load((overlay / "config" / "agents.yaml").read_text())
    assert set(cfg["agents"]) == {"main", "helper"}


def test_invalid_inputs(overlay):
    with pytest.raises(ValueError, match="Invalid agent name"):
        scaffold_agent(overlay, "Bad Name!", "X", "Y")
    with pytest.raises(ValueError, match="Invalid tier"):
        scaffold_agent(overlay, "okname", "X", "Y", tier="root")
    with pytest.raises(ValueError, match="config directory not found"):
        scaffold_agent(overlay / "nope", "okname", "X", "Y")


def test_custom_agents_file_and_profile(overlay):
    scaffold_agent(
        overlay, "rescue", "Rescue", "Ops", tier="privileged",
        agents_file="agents.rescue.yaml", profile="rescue",
        claude_md="# Custom rescue prompt\n",
    )
    assert (overlay / "config" / "agents.rescue.yaml").exists()
    assert not (overlay / "config" / "agents.yaml").exists()
    assert (overlay / "agents" / "rescue" / "AGENTS.md").read_text() == "# Custom rescue prompt\n"
    assert (overlay / "agents" / "rescue" / "CLAUDE.md").read_text().startswith("@AGENTS.md")
    mcp = json.loads((overlay / "agents" / "rescue" / ".mcp.json").read_text())
    assert mcp["mcpServers"]["kbots-tools"]["env"]["KBOTS_PROFILE"] == "rescue"


def test_routing_override(overlay):
    routing = {"discord": {"account": "ops", "channels": ["123"], "mentions": False}}
    scaffold_agent(overlay, "router", "R", "Routed", routing=routing)
    cfg = yaml.safe_load((overlay / "config" / "agents.yaml").read_text())
    assert cfg["agents"]["router"]["routing"] == routing
    # The routing's account wins over the bot_account default in .mcp.json
    mcp = json.loads((overlay / "agents" / "router" / ".mcp.json").read_text())
    assert mcp["mcpServers"]["kbots-tools"]["env"]["KBOTS_BOT_ACCOUNT"] == "ops"


def test_write_identity_creates_canonical_plus_stub(tmp_path):
    from src.core.agent_scaffold import read_identity, write_identity
    written = write_identity(tmp_path, "# Soul\n")
    assert {p.name for p in written} == {"AGENTS.md", "CLAUDE.md"}
    assert (tmp_path / "AGENTS.md").read_text() == "# Soul\n"
    assert (tmp_path / "CLAUDE.md").read_text().startswith("@AGENTS.md")
    # No overwrite without force
    assert write_identity(tmp_path, "# Other\n") == []
    assert (tmp_path / "AGENTS.md").read_text() == "# Soul\n"
    # Force replaces both
    write_identity(tmp_path, "# Other\n", force=True)
    assert (tmp_path / "AGENTS.md").read_text() == "# Other\n"
    assert read_identity(tmp_path) == "# Other\n"


def test_read_identity_falls_back_to_legacy_claude_md(tmp_path):
    from src.core.agent_scaffold import read_identity
    assert read_identity(tmp_path) == ""
    (tmp_path / "CLAUDE.md").write_text("# Legacy identity\n")
    assert read_identity(tmp_path) == "# Legacy identity\n"
    # AGENTS.md wins once present
    (tmp_path / "AGENTS.md").write_text("# Canonical\n")
    assert read_identity(tmp_path) == "# Canonical\n"
