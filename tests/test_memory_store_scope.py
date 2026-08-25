"""memory_store scope parameter — agents choosing private vs shared memories.

The default changed on 2026-08-22. It used to file a memory under the agent AND
hide it from every other agent, and since no caller ever passed a scope, 235 of
237 memories on the live store were unreadable by anyone but their author. The
default now files under the author and shares; `private` is the opt-out.
"""

from src.core.base import ToolContext
from src.memory.sqlite import SQLiteMemory
from src.tools.memory import memory_store


def _ctx(mem, agent_id="alice"):
    return ToolContext(agent_id=agent_id, memory=mem)


async def test_store_default_scope_is_authored_and_shared(tmp_path):
    """Filed under its author, readable by the fleet."""
    mem = SQLiteMemory({"path": str(tmp_path / "m.db")})
    try:
        out = await memory_store(_ctx(mem), "alice's working note")
        assert "scope: agent" in out
        row = mem.db.execute("SELECT scope, scope_target FROM memories").fetchone()
        assert row["scope"] == "agent:alice" and row["scope_target"] == "alice"
        rows = await mem.list_since("bob", limit=10)
        assert [m["content"] for m in rows] == ["alice's working note"]
        assert rows[0]["created_by"] == "alice", "sharing must not cost provenance"
    finally:
        mem.close()


async def test_store_private_scope_stays_private(tmp_path):
    """The opt-out. Without it, sharing by default has no safety valve."""
    mem = SQLiteMemory({"path": str(tmp_path / "m.db")})
    try:
        out = await memory_store(_ctx(mem), "alice's working note", scope="private")
        assert "scope: private" in out
        row = mem.db.execute("SELECT scope, scope_target FROM memories").fetchone()
        assert row["scope"] == "private:alice" and row["scope_target"] == "alice"
        assert await mem.list_since("bob", limit=10) == []
        assert len(await mem.list_since("alice", limit=10)) == 1
    finally:
        mem.close()


async def test_store_global_scope_is_fleet_visible(tmp_path):
    mem = SQLiteMemory({"path": str(tmp_path / "m.db")})
    try:
        out = await memory_store(_ctx(mem), "the deploy day is Friday", scope="global")
        assert "scope: global" in out
        rows = await mem.list_since("bob", limit=10)
        assert [m["content"] for m in rows] == ["the deploy day is Friday"]
        # provenance survives sharing
        assert rows[0]["created_by"] == "alice"
    finally:
        mem.close()


async def test_store_group_scope_reaches_members_only(tmp_path):
    """`group:%` used to match for every agent, so a group scope was a global
    scope with a reassuring name. Membership now comes from config.
    """
    mem = SQLiteMemory({"path": str(tmp_path / "m.db"),
                        "fleet_read": False, "groups": {"ops": ["bob"]}})
    try:
        out = await memory_store(_ctx(mem), "ops runbook fact", scope="group:ops")
        assert "scope: group:ops" in out
        assert len(await mem.list_since("bob", limit=10)) == 1
        assert await mem.list_since("carol", limit=10) == []
    finally:
        mem.close()


async def test_store_rejects_invalid_scope(tmp_path):
    mem = SQLiteMemory({"path": str(tmp_path / "m.db")})
    try:
        out = await memory_store(_ctx(mem), "x", scope="everyone")
        assert "Invalid scope" in out
        assert mem.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    finally:
        mem.close()
