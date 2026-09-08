"""list_by_category(own_only=True) returns what the agent itself saved.

With fleet_read on (the default) the plain listing is fleet-wide, which is
right for recall and wrong for the reflector: it turned every agent's
LESSONS.md into a digest of everyone's lessons.
"""

import pytest

from src.memory.sqlite import SQLiteMemory


@pytest.mark.asyncio
async def test_own_only_excludes_other_agents_lessons(tmp_path):
    mem = SQLiteMemory({"path": str(tmp_path / "m.db")})
    await mem.store("a one", "semantic", agent_id="a", category="lesson",
                    scope="agent", scope_target="a")
    await mem.store("a two", "semantic", agent_id="a", category="lesson",
                    scope="private", scope_target="a")
    await mem.store("fleet-wide", "semantic", agent_id="b", category="lesson",
                    scope="global")
    for i in range(5):
        await mem.store(f"b {i}", "semantic", agent_id="b", category="lesson",
                        scope="agent", scope_target="b")
    await mem.store("not a lesson", "semantic", agent_id="a", category="general",
                    scope="agent", scope_target="a")

    fleet = await mem.list_by_category("a", "lesson")
    own = await mem.list_by_category("a", "lesson", own_only=True)

    assert len(fleet) == 8                       # the default read is fleet-wide
    assert sorted(m["content"] for m in own) == ["a one", "a two", "fleet-wide"]

    # an agent with nothing of its own sees only global, however much the
    # fleet has saved
    assert [m["content"] for m in await mem.list_by_category("c", "lesson", own_only=True)] \
        == ["fleet-wide"]
