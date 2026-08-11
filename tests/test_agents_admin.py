"""Tests for the create_agent tool — one Discord app per agent."""

import yaml

from src.core.base import ToolContext
from src.tools.agents_admin import create_agent


def _write_config(overlay, guild_id="111222333"):
    cfg = {
        "connectors": {
            "discord": {
                "enabled": True,
                "guild_id": guild_id,
                "accounts": {"main": {"token_key": "discord-token"}},
            }
        }
    }
    (overlay / "config" / "config.yaml").write_text(yaml.dump(cfg))


def _write_main_agent(overlay, extra_agents: dict | None = None):
    """The wizard-created main agent (coordinator), bound to the 'main' bot."""
    agents = {
        "agents": {
            "main": {
                "display_name": "MAIN",
                "tier": "coordinator",
                "routing": {"discord": {"account": "main", "channels": [], "mentions": True}},
            },
            **(extra_agents or {}),
        }
    }
    (overlay / "config" / "agents.yaml").write_text(yaml.dump(agents))


def _ctx():
    return ToolContext(agent_id="main")


async def test_default_account_is_agent_name_with_portal_prompt(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    _write_config(overlay, guild_id="999888777")
    _write_main_agent(overlay)

    result = await create_agent(
        _ctx(), "databot", "Data.Bot", "Research agent",
    )

    # Own identity by default: account = agent name, portal prompt included
    assert 'named "Data.Bot"' in result
    assert "discord.com/channels/999888777" in result
    assert "Message Content Intent" in result
    assert "do NOT read, copy" in result       # token stays human-handled
    assert "vault-manage.py" in result and "discord-databot" in result

    # Account auto-registered in config.yaml, agent routed through it
    cfg = yaml.safe_load((overlay / "config" / "config.yaml").read_text())
    assert cfg["connectors"]["discord"]["accounts"]["databot"] == {"token_key": "discord-databot"}
    agents = yaml.safe_load((overlay / "config" / "agents.yaml").read_text())
    assert agents["agents"]["databot"]["routing"]["discord"]["account"] == "databot"


async def test_refuses_account_used_by_another_agent(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    _write_config(overlay)
    _write_main_agent(overlay)

    result = await create_agent(
        _ctx(), "helper", "Helper", "Tries to share the main bot",
        bot_account="main",
    )
    assert result.startswith("ERROR")
    assert "one Discord app per agent" in result
    # Nothing scaffolded
    agents = yaml.safe_load((overlay / "config" / "agents.yaml").read_text())
    assert "helper" not in agents["agents"]


async def test_pre_registered_unused_account_skips_portal(overlay, monkeypatch):
    """An account already in config but bound to no agent needs no portal work."""
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    cfg = {
        "connectors": {
            "discord": {
                "enabled": True,
                "guild_id": "1",
                "accounts": {
                    "main": {"token_key": "discord-token"},
                    "spare": {"token_key": "discord-spare"},
                },
            }
        }
    }
    (overlay / "config" / "config.yaml").write_text(yaml.dump(cfg))
    _write_main_agent(overlay)

    result = await create_agent(
        _ctx(), "quest", "Quest", "Uses the spare bot", bot_account="spare",
    )
    assert "pre-registered bot account 'spare'" in result
    assert "Developer Portal" not in result
    assert "discord-spare" in result


async def test_new_bot_without_guild_id_asks_user(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    _write_config(overlay, guild_id="")
    _write_main_agent(overlay)

    result = await create_agent(
        _ctx(), "solo", "Solo", "Agent",
    )
    assert "ask me which one" in result


async def test_missing_overlay_env(monkeypatch):
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    result = await create_agent(_ctx(), "x", "X", "Y")
    assert result.startswith("ERROR")


async def test_assistant_caller_refused(overlay, monkeypatch):
    """Assistants cannot use the orchestrator's admin tools."""
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    _write_config(overlay)
    _write_main_agent(overlay, extra_agents={
        "helper": {
            "display_name": "Helper",
            "tier": "assistant",
            "routing": {"discord": {"account": "helper", "channels": [], "mentions": True}},
        }
    })

    result = await create_agent(
        ToolContext(agent_id="helper"), "sneaky", "Sneaky", "Created by an assistant",
    )
    assert result.startswith("ERROR")
    assert "not allowed to create agents" in result
    agents = yaml.safe_load((overlay / "config" / "agents.yaml").read_text())
    assert "sneaky" not in agents["agents"]


async def test_unknown_caller_refused(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    _write_config(overlay)
    _write_main_agent(overlay)

    result = await create_agent(
        ToolContext(agent_id="ghost"), "x", "X", "Y",
    )
    assert result.startswith("ERROR")
    assert "not allowed to create agents" in result


async def test_privileged_caller_allowed(overlay, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    _write_config(overlay)
    _write_main_agent(overlay, extra_agents={
        "engineer": {
            "display_name": "Engineer",
            "privileged": True,
            "routing": {"discord": {"account": "engineer", "channels": [], "mentions": True}},
        }
    })

    result = await create_agent(
        ToolContext(agent_id="engineer"), "newbie", "Newbie", "Created by ops agent",
    )
    assert not result.startswith("ERROR")
    assert "Newbie" in result
