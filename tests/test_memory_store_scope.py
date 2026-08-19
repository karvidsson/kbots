"""memory_store scope parameter — agents choosing private vs shared memories."""

from src.core.base import ToolContext
from src.memory.sqlite import SQLiteMemory
from src.tools.memory import memory_store


def _ctx(mem, agent_id="alice"):
    return ToolContext(agent_id=agent_id, memory=mem)


async def test_store_default_scope_stays_private(tmp_path):
    mem = SQLiteMemory({"path": str(tmp_path / "m.db")})
    try:
        out = await memory_store(_ctx(mem), "alice's working note")
        assert "scope: agent" in out
        row = mem.db.execute("SELECT scope, scope_target FROM memories").fetchone()
        assert row["scope"] == "agent:alice" and row["scope_target"] == "alice"
        # invisible to another agent
        assert await mem.list_since("bob", limit=10) == []
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


async def test_store_group_scope(tmp_path):
    mem = SQLiteMemory({"path": str(tmp_path / "m.db")})
    try:
        out = await memory_store(_ctx(mem), "ops runbook fact", scope="group:ops")
        assert "scope: group:ops" in out
        assert len(await mem.list_since("bob", limit=10)) == 1
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
