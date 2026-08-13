"""Tests for src/core/agent_scaffold.py — shared by setup wizard and create_agent tool."""

import json
from pathlib import Path

import pytest
import yaml

from src.core.agent_scaffold import cc_allow_for_tier, default_claude_md, scaffold_agent


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


def test_assistant_can_reach_the_shared_temp_dir(overlay, tmp_path, monkeypatch):
    """Regression: agents were told to write to $KBOTS_TMP but not allowed to read it.

    The media tools (screenshots, generated images, charts) write there, and
    viewing an image needs the native Read — so a folder-scoped agent could
    call a tool successfully and then be denied the file it had just produced.
    """
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("KBOTS_TMP", str(scratch))

    scaffold_agent(overlay, "worker", "Worker", "Scoped agent", tier="assistant")
    allow = json.loads(
        (overlay / "agents" / "worker" / ".claude" / "settings.json").read_text()
    )["permissions"]["allow"]

    for verb in ("Read", "Write", "Edit", "MultiEdit", "Glob", "Grep"):
        assert f"{verb}({scratch}/**)" in allow, f"{verb} on $KBOTS_TMP missing"
    # Widening the scratch dir must not have widened anything else.
    assert "Read(*)" not in allow
    assert "Write(*)" not in allow


def test_temp_dir_rules_are_derived_not_hardcoded(monkeypatch, tmp_path):
    """The path must follow the environment — installs put the overlay anywhere.

    Also covers the KBOTS_OVERLAY fallback, since most installs set only that.
    """
    monkeypatch.setenv("KBOTS_TMP", str(tmp_path / "explicit"))
    assert f"Read({tmp_path / 'explicit'}/**)" in cc_allow_for_tier("assistant")

    monkeypatch.delenv("KBOTS_TMP")
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path / "ovl"))
    assert f"Read({tmp_path / 'ovl' / 'tmp'}/**)" in cc_allow_for_tier("assistant")


def test_coordinator_gains_temp_writes_but_not_extra_reads(monkeypatch, tmp_path):
    """Coordinator already reads everywhere; only the write side was missing."""
    monkeypatch.setenv("KBOTS_TMP", str(tmp_path / "scratch"))
    allow = cc_allow_for_tier("coordinator")
    assert f"Write({tmp_path / 'scratch'}/**)" in allow
    assert "Read(*)" in allow  # reads were never the gap


def test_privileged_tier_needs_no_temp_rules(monkeypatch, tmp_path):
    """Unrestricted already — adding scoped rules would only imply otherwise."""
    monkeypatch.setenv("KBOTS_TMP", str(tmp_path / "scratch"))
    assert not [r for r in cc_allow_for_tier("privileged") if "scratch" in r]


def test_agent_brief_points_at_the_roster_tool_not_a_file(tmp_path):
    """Regression: the brief named a roster path agents could never open.

    It was relative, so it resolved inside the agent's own folder where no such
    file exists, and the real one sits outside the sandbox. An agent that went
    looking found nothing and started reading around outside its sandbox.
    """
    brief = default_claude_md("Worker", "Does things", tmp_path)
    assert "team_list" in brief
    assert "config/team.json" not in brief


@pytest.mark.parametrize("module", ["src/core/agent_scaffold.py", "scripts/settings.py"])
def test_no_template_still_points_at_the_roster_file(module):
    """The brief template is duplicated across the two agent-creation paths.

    scripts/settings.py (interactive setup) carries its own copy rather than
    calling default_claude_md, so fixing one leaves the other handing new
    agents the same dead path. Pin both until they are merged.
    """
    source = (Path(__file__).resolve().parent.parent / module).read_text()
    assert "The team roster is at" not in source, (
        f"{module} still points agents at a roster file path")
    assert "team_list" in source, f"{module} does not mention the roster tool"


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
