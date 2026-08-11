"""Reward-based lessons: remember_lesson + record_outcome confidence math."""

from src.core.base import ToolContext
from src.tools.lessons import record_outcome, remember_lesson


class FakeMemory:
    def __init__(self):
        self.items = {}
        self._n = 0

    async def store(self, content, type, agent_id=None, tags=None, **kw):
        self._n += 1
        mid = f"m{self._n}"
        self.items[mid] = {
            "id": mid, "content": content, "type": type,
            "category": kw.get("category"), "confidence": kw.get("confidence", 0.7),
            "tags": tags or [],
        }
        return mid

    async def get(self, mid):
        return self.items.get(mid)

    async def search(self, query, agent_id=None, limit=5, **kw):
        hits = [m for m in self.items.values() if query.lower() in m["content"].lower()]
        return hits[:limit]

    async def update(self, mid, agent_id=None, **fields):
        self.items[mid].update(fields)
        return True


def _ctx():
    return ToolContext(agent_id="a", memory=FakeMemory())


async def test_remember_lesson_categorised():
    ctx = _ctx()
    out = await remember_lesson(ctx, "use tool X for task Y")
    assert "Lesson saved" in out
    m = next(iter(ctx.memory.items.values()))
    assert m["category"] == "lesson" and "lesson" in m["tags"] and m["type"] == "procedural"


async def test_remember_lesson_requires_content():
    assert (await remember_lesson(_ctx(), "  ")).startswith("ERROR")


async def test_useful_promotes_to_auto_inject():
    ctx = _ctx()
    await remember_lesson(ctx, "approach alpha works well")
    out = await record_outcome(ctx, "alpha", "useful")
    assert ctx.memory.items["m1"]["confidence"] == 0.8   # 0.7 + 0.1, rounded — crosses 0.8
    assert "0.70 → 0.80" in out and "auto-surface" in out


async def test_dead_end_demotes():
    ctx = _ctx()
    await remember_lesson(ctx, "approach beta")
    await record_outcome(ctx, "beta", "dead_end")
    assert ctx.memory.items["m1"]["confidence"] == 0.5   # 0.7 - 0.2


async def test_corrected_appends_and_demotes():
    ctx = _ctx()
    await remember_lesson(ctx, "the limit is 100")
    out = await record_outcome(ctx, "limit", "corrected", correction="the limit is 250")
    m = ctx.memory.items["m1"]
    assert m["confidence"] == 0.5
    assert "[CORRECTION] the limit is 250" in m["content"]
    assert "demoted" in out


async def test_outcome_by_id():
    ctx = _ctx()
    await remember_lesson(ctx, "gamma path")
    await record_outcome(ctx, "m1", "useful")   # id directly
    assert ctx.memory.items["m1"]["confidence"] == 0.8


async def test_invalid_outcome():
    assert (await record_outcome(_ctx(), "x", "maybe")).startswith("ERROR")


async def test_not_found():
    assert "No matching lesson" in await record_outcome(_ctx(), "nothing", "useful")


async def test_confidence_clamped():
    ctx = _ctx()
    await remember_lesson(ctx, "delta")
    for _ in range(6):
        await record_outcome(ctx, "m1", "useful")   # +0.6 → would exceed cap
    assert ctx.memory.items["m1"]["confidence"] <= 0.99
