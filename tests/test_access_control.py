"""Tests for src.core.access_control — three-layer permission system.

Layer 1: can_message (who can talk to whom)
Layer 2: tool gating (what tools per sender)
Layer 3: agent ceiling (tested via agents.yaml, not here)
"""

import json
from unittest.mock import patch

import pytest

from src.core.access_control import AccessControl

# --- Fixtures ---

TEAM_DATA = {
    "humans": [
        {"id": "alice", "name": "Alice", "type": "human", "access": "owner",
         "contact": {"discord": "111"}},
        {"id": "bob", "name": "Bob", "type": "human", "access": "admin",
         "contact": {"discord": "222"}},
        {"id": "staff1", "name": "Staff User", "type": "human", "access": "staff",
         "contact": {"discord": "333"}},
    ],
    "agents": [
        {"id": "coordinator", "name": "Coordinator", "type": "agent",
         "agent_tier": "coordinator", "discord": "444"},
        {"id": "ops", "name": "Ops Bot", "type": "agent",
         "agent_tier": "privileged", "discord": "555"},
        {"id": "assistant", "name": "Assistant", "type": "agent",
         "agent_tier": "assistant", "discord": "666"},
    ],
}

CONFIG = {
    "safe_tools": [
        "memory_search", "team_list", "team_get",
        "read_channel_history", "web_search",
    ],
    "unknown_policy": "deny",
}

ALL_TOOLS = [
    "memory_search", "memory_store", "memory_forget",
    "team_list", "team_get", "team_update",
    "send_message", "send_to_agent", "ask_agent",
    "read_channel_history", "web_search",
    "install_mcp", "send_email",
]


@pytest.fixture
def team_file(tmp_path):
    f = tmp_path / "team.json"
    f.write_text(json.dumps(TEAM_DATA))
    return f


@pytest.fixture
def ac(team_file):
    with patch("src.core.access_control.TEAM_FILE", team_file):
        return AccessControl(CONFIG)


# ============================================================
# Layer 1: can_message — who can talk to whom
# ============================================================


class TestCanMessageOwner:
    """Owner (Alice) can message all agent tiers."""
    def test_owner_to_privileged(self, ac):
        assert ac.can_message("111", "ops") is True

    def test_owner_to_coordinator(self, ac):
        assert ac.can_message("111", "coordinator") is True

    def test_owner_to_assistant(self, ac):
        assert ac.can_message("111", "assistant") is True


class TestCanMessageAdmin:
    """Admin (Bob) can message privileged + coordinator + assistant (same reach as owner)."""
    def test_admin_to_privileged(self, ac):
        assert ac.can_message("222", "ops") is True

    def test_admin_to_coordinator(self, ac):
        assert ac.can_message("222", "coordinator") is True

    def test_admin_to_assistant(self, ac):
        assert ac.can_message("222", "assistant") is True


class TestCanMessageStaff:
    """Staff (staff1) can message assistant only."""
    def test_staff_to_privileged(self, ac):
        assert ac.can_message("333", "ops") is False

    def test_staff_to_coordinator(self, ac):
        assert ac.can_message("333", "coordinator") is False

    def test_staff_to_assistant(self, ac):
        assert ac.can_message("333", "assistant") is True


class TestCanMessageUnknown:
    """Unknown users can't message anyone."""
    def test_unknown_to_privileged(self, ac):
        assert ac.can_message("999", "ops") is False

    def test_unknown_to_coordinator(self, ac):
        assert ac.can_message("999", "coordinator") is False

    def test_unknown_to_assistant(self, ac):
        assert ac.can_message("999", "assistant") is False


class TestCanMessageAgentToAgent:
    """Agent-to-agent messaging rules."""
    def test_privileged_to_coordinator(self, ac):
        assert ac.can_message("555", "coordinator", is_bot=True) is True

    def test_privileged_to_assistant(self, ac):
        assert ac.can_message("555", "assistant", is_bot=True) is True

    def test_coordinator_to_privileged(self, ac):
        assert ac.can_message("444", "ops", is_bot=True) is True

    def test_coordinator_to_assistant(self, ac):
        assert ac.can_message("444", "assistant", is_bot=True) is True

    def test_assistant_to_coordinator(self, ac):
        """Assistant cannot message any agents."""
        assert ac.can_message("666", "coordinator", is_bot=True) is False

    def test_assistant_to_privileged(self, ac):
        assert ac.can_message("666", "ops", is_bot=True) is False

    def test_unknown_bot(self, ac):
        assert ac.can_message("999", "coordinator", is_bot=True) is False


# ============================================================
# Layer 2a: Tool gating (check method)
# ============================================================


class TestCheckOwner:
    def test_safe_tool(self, ac):
        assert ac.check("111", "memory_search")["allowed"] is True

    def test_write_tool(self, ac):
        assert ac.check("111", "send_message")["allowed"] is True

    def test_dangerous_tool(self, ac):
        assert ac.check("111", "install_mcp")["allowed"] is True


class TestCheckAdmin:
    def test_safe_tool(self, ac):
        assert ac.check("222", "memory_search")["allowed"] is True

    def test_non_safe_allowed(self, ac):
        # Admin gets through AC; existing HITL config gates dangerous tools
        assert ac.check("222", "send_message")["allowed"] is True


class TestCheckStaff:
    def test_safe_tool(self, ac):
        assert ac.check("333", "memory_search")["allowed"] is True

    def test_write_denied(self, ac):
        r = ac.check("333", "send_message")
        assert r["allowed"] is False
        assert r["gate"] == "deny"


class TestCheckAgent:
    def test_safe_tool(self, ac):
        assert ac.check("444", "memory_search", is_bot=True)["allowed"] is True

    def test_write_hitl(self, ac):
        r = ac.check("444", "send_message", is_bot=True)
        assert r["allowed"] is False
        assert r["gate"] == "hitl"


class TestCheckAssistantIsolation:
    def test_safe_tool(self, ac):
        assert ac.check("666", "memory_search", is_bot=True,
                         agent_id="assistant")["allowed"] is True

    def test_send_message_denied(self, ac):
        r = ac.check("666", "send_message", is_bot=True, agent_id="assistant")
        assert r["allowed"] is False
        assert r["gate"] == "deny"

    def test_send_to_agent_denied(self, ac):
        r = ac.check("666", "send_to_agent", is_bot=True, agent_id="assistant")
        assert r["allowed"] is False
        assert r["gate"] == "deny"

    def test_coordinator_not_isolated(self, ac):
        """Coordinator agent can use send_message (gets HITL, not deny)."""
        r = ac.check("444", "send_message", is_bot=True, agent_id="coordinator")
        assert r["gate"] == "hitl"


class TestCheckUnknown:
    def test_safe_denied(self, ac):
        r = ac.check("999", "memory_search")
        assert r["allowed"] is False

    def test_write_denied(self, ac):
        r = ac.check("999", "send_message")
        assert r["allowed"] is False


# ============================================================
# Layer 2b: --disallowedTools computation
# ============================================================


class TestDisallowedToolsForSender:
    def test_owner_no_blocks(self, ac):
        assert ac.disallowed_tools_for_sender("111", ALL_TOOLS) == []

    def test_admin_no_blocks(self, ac):
        assert ac.disallowed_tools_for_sender("222", ALL_TOOLS) == []

    def test_staff_blocks_non_safe(self, ac):
        blocked = ac.disallowed_tools_for_sender("333", ALL_TOOLS)
        assert "memory_search" not in blocked
        assert "web_search" not in blocked
        assert "send_message" in blocked
        assert "install_mcp" in blocked

    def test_agent_blocks_non_safe(self, ac):
        blocked = ac.disallowed_tools_for_sender(
            "444", ALL_TOOLS, is_bot=True, agent_id="coordinator")
        assert "memory_search" not in blocked
        assert "send_message" in blocked

    def test_assistant_blocks_cross_agent(self, ac):
        blocked = ac.disallowed_tools_for_sender(
            "666", ALL_TOOLS, is_bot=True, agent_id="assistant")
        assert "send_message" in blocked
        assert "send_to_agent" in blocked
        assert "ask_agent" in blocked

    def test_owner_with_assistant_agent(self, ac):
        """Owner triggering assistant still blocks cross-agent."""
        blocked = ac.disallowed_tools_for_sender(
            "111", ALL_TOOLS, agent_id="assistant")
        assert "send_message" in blocked
        assert "send_to_agent" in blocked
        assert "memory_store" not in blocked

    def test_unknown_blocks_everything(self, ac):
        blocked = ac.disallowed_tools_for_sender("999", ALL_TOOLS)
        assert set(blocked) == set(ALL_TOOLS)


class TestDisallowedBuiltinsForSender:
    def test_owner_no_blocks(self, ac):
        assert ac.disallowed_builtins_for_sender("111") == []

    def test_admin_blocked(self, ac):
        # Admin: Bash blocked, but Edit/Write/MultiEdit allowed (per access_control.py).
        blocked = ac.disallowed_builtins_for_sender("222")
        assert blocked == ["Bash"]

    def test_staff_blocked(self, ac):
        blocked = ac.disallowed_builtins_for_sender("333")
        assert len(blocked) == 4

    def test_agent_blocked(self, ac):
        blocked = ac.disallowed_builtins_for_sender("444", is_bot=True)
        assert "Bash" in blocked

    def test_unknown_blocked(self, ac):
        blocked = ac.disallowed_builtins_for_sender("999")
        assert len(blocked) == 4


# ============================================================
# Tier resolution
# ============================================================


class TestResolveTier:
    def test_owner(self, ac):
        assert ac.resolve_tier("111") == "owner"

    def test_admin(self, ac):
        assert ac.resolve_tier("222") == "admin"

    def test_staff(self, ac):
        assert ac.resolve_tier("333") == "staff"

    def test_agent(self, ac):
        assert ac.resolve_tier("444") == "agent"

    def test_unknown(self, ac):
        assert ac.resolve_tier("999") == "unknown"

    def test_human_with_bot_flag(self, ac):
        assert ac.resolve_tier("222", is_bot=True) == "agent"


class TestAgentTierResolution:
    def test_coordinator(self, ac):
        assert ac.get_agent_tier("coordinator") == "coordinator"

    def test_privileged(self, ac):
        assert ac.get_agent_tier("ops") == "privileged"

    def test_assistant(self, ac):
        assert ac.get_agent_tier("assistant") == "assistant"

    def test_unknown_defaults_assistant(self, ac):
        assert ac.get_agent_tier("nonexistent") == "assistant"


# ============================================================
# Edge cases
# ============================================================


class TestEdgeCases:
    def test_reload_team(self, ac, team_file):
        data = json.loads(team_file.read_text())
        data["humans"].append({
            "id": "new", "name": "New", "type": "human",
            "access": "staff", "contact": {"discord": "777"},
        })
        team_file.write_text(json.dumps(data))
        assert ac.resolve_tier("777") == "unknown"
        with patch("src.core.access_control.TEAM_FILE", team_file):
            ac.reload_team()
        assert ac.resolve_tier("777") == "staff"

    def test_missing_team_file(self, tmp_path):
        with patch("src.core.access_control.TEAM_FILE", tmp_path / "nope.json"):
            ac = AccessControl(CONFIG)
        assert ac.resolve_tier("111") == "unknown"
        assert ac.can_message("111", "coordinator") is False

    def test_empty_config(self, team_file):
        with patch("src.core.access_control.TEAM_FILE", team_file):
            ac = AccessControl({})
        assert ac.check("111", "send_message")["allowed"] is True
        r = ac.check("444", "memory_search", is_bot=True)
        assert r["allowed"] is False

    def test_unknown_safe_only_policy(self, team_file):
        cfg = {**CONFIG, "unknown_policy": "safe_only"}
        with patch("src.core.access_control.TEAM_FILE", team_file):
            ac = AccessControl(cfg)
        assert ac.check("999", "memory_search")["allowed"] is True
        assert ac.check("999", "send_message")["allowed"] is False
