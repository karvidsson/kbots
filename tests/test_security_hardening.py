"""Regression tests for the Phase-1 security hardening.

Covers the fail-closed behaviours that were previously fail-open:
- HITL denies gated / hitl=True tools when no channel is configured
- empty `approvers` means nobody can approve (falls back to admin_users)
- access control treats configured admins as owner even absent from team.json
"""

import aiosqlite
import pytest

from src.core.access_control import AccessControl
from src.core.hitl import HITLGate
from src.mcp_server import MCPHitlGate


class _FakeVault:
    def get(self, key):
        return None


# --- In-process HITL gate (src/core/hitl.py) ---

@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


async def test_hitl_no_channel_denies(tmp_path, overlay):
    """A gated tool must be denied (not silently run) when no channel is set."""
    db = await aiosqlite.connect(tmp_path / "t.db")
    g = HITLGate({"gated_tools": ["send_email"], "fail_mode": "closed"}, db)
    await g.init_schema()
    result = await g.request_approval("agent", "send_email", {"to": "x"}, "desc")
    assert result["status"] == "denied"
    assert result["reason"] == "no_channel"
    await db.close()


async def test_hitl_empty_approvers_denies_approval(tmp_path, overlay):
    """Empty approvers must mean nobody can approve — not everyone."""
    db = await aiosqlite.connect(tmp_path / "t.db")
    g = HITLGate({"channel": "c"}, db)  # no approvers, no admin fallback
    await g.init_schema()
    assert g.approvers == set()
    assert await g.approve("someid", "999") is False
    await db.close()


async def test_hitl_admin_users_fallback_approves(tmp_path, overlay):
    """With no explicit approvers, admin_users become the approvers."""
    db = await aiosqlite.connect(tmp_path / "t.db")
    g = HITLGate({"channel": "c"}, db, admin_users=["111"])
    await g.init_schema()
    assert g.approvers == {"111"}
    # Insert a pending row so approve() can resolve it.
    import time
    import uuid
    hid = str(uuid.uuid4())[:8]
    await db.execute(
        "INSERT INTO hitl_pending (hitl_id, agent_id, tool_name, args_json, description, "
        "channel_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (hid, "a", "send_email", "{}", "d", "c", time.time()))
    await db.commit()
    assert await g.approve(hid, "999") is False   # non-admin
    assert await g.approve(hid, "111") is True     # admin
    await db.close()


# --- MCP HITL gate (src/mcp_server.py) ---

async def test_mcp_hitl_no_channel_denies():
    g = MCPHitlGate({"gated_tools": ["send_email"], "fail_mode": "closed"}, _FakeVault())
    result = await g.request_approval("send_email", {"to": "x"})
    assert result["status"] == "denied"


def test_mcp_hitl_admin_fallback():
    g = MCPHitlGate({}, _FakeVault(), admin_users=["111", "222"])
    assert g.approvers == {"111", "222"}


# --- Access control admin bridge (src/core/access_control.py) ---

def test_admin_user_resolves_to_owner(monkeypatch):
    """A configured admin is owner even if absent from team.json — this is what
    keeps enabling access control from locking the operator out."""
    ac = AccessControl({}, admin_users=["111"])
    # Force an empty team so the admin isn't found there.
    ac._team = {}
    ac._agent_tiers = {}
    assert ac.resolve_tier("111") == "owner"
    assert ac.resolve_tier("999") == "unknown"


def test_admin_owner_can_message_and_use_tools(monkeypatch):
    ac = AccessControl({}, admin_users=["111"])
    ac._team = {}
    ac._agent_tiers = {"bot": "privileged"}
    assert ac.can_message("111", "bot") is True
    assert ac.check("111", "run_command", agent_id="bot")["allowed"] is True
    # A non-admin unknown user is denied a non-safe tool.
    assert ac.check("999", "run_command", agent_id="bot")["allowed"] is False


# --- HITL denial messages: the agent must be able to tell the user WHY ---

def test_hitl_no_channel_message_names_the_setting():
    """A denial caused by missing config must say so — the agent relays this to
    the user, who otherwise can't distinguish it from a human saying no."""
    from src.core.hitl import hitl_result_message
    msg = hitl_result_message("send_email", {"status": "denied", "reason": "no_channel"})
    assert "security.hitl.channel" in msg
    assert "did NOT run" in msg
    # The MCP-side gate uses a different reason string for the same condition.
    msg = hitl_result_message(
        "send_email", {"status": "denied", "reason": "no discord token or HITL channel"})
    assert "security.hitl.channel" in msg


def test_hitl_human_denial_message_stays_generic():
    from src.core.hitl import hitl_result_message
    for outcome in ({"status": "denied", "approver": "111"}, {"status": "timeout"}):
        msg = hitl_result_message("send_email", outcome)
        assert outcome["status"] in msg
        assert "security.hitl.channel" not in msg
