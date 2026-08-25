"""Entity anchors — the join between the sqlite store and the graph.

Before this table existed the two stores could only be related by matching
names in prose. A memory found by search offered no way into the graph, and an
entity found by traversal offered no way back to the facts that mention it, so
the graph was write-only in practice: the reflector spent an LLM call per batch
building it and nothing ever read it.

The reflector was already resolving every extracted edge to the memory id it
came from, and throwing that link away.
"""

import asyncio

import pytest


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def anchored(memory):
    async def go():
        mid = await memory.store(content="Blue Fox publishes on Shortform.",
                                 type="semantic", agent_id="fox", scope="global")
        await memory.anchor_entities(mid, ["Blue Fox", "Shortform"])
        return mid
    return memory, run(go())


def test_an_anchored_memory_is_reachable_from_its_entities(anchored):
    memory, mid = anchored
    hits = run(memory.memories_for_entities(["Shortform"], agent_id="fox"))
    assert [h["id"] for h in hits] == [mid]


def test_entities_are_reachable_from_the_memory(anchored):
    memory, mid = anchored
    assert set(run(memory.entities_for_memories([mid]))) == {"Blue Fox", "Shortform"}


def test_lookup_is_by_canonical_key_not_by_spelling(anchored):
    """Traversal returns whatever spelling the graph stored. If the join were
    by exact string, a graph that says 'blue-fox' could not find a memory
    anchored as 'Blue Fox' and the hop would return nothing for the entities
    most likely to be duplicated.
    """
    memory, mid = anchored
    for spelling in ("blue-fox", "BLUE FOX", "Blue  Fox"):
        hits = run(memory.memories_for_entities([spelling], agent_id="fox"))
        assert [h["id"] for h in hits] == [mid], f"{spelling!r} did not resolve"


def test_anchoring_is_idempotent(anchored):
    """Re-extraction re-asserts the same anchors on every reflection pass."""
    memory, mid = anchored
    run(memory.anchor_entities(mid, ["Blue Fox", "blue fox", "Shortform"]))
    rows = memory.db.execute(
        "SELECT COUNT(*) FROM memory_entities WHERE memory_id = ?", (mid,)).fetchone()
    assert rows[0] == 2


def test_anchors_with_no_alphanumerics_are_rejected_not_stored(memory):
    async def go():
        mid = await memory.store(content="x", type="semantic", agent_id="t")
        return await memory.anchor_entities(mid, ["???", "", "  "])
    assert run(go()) == 0


def test_entity_lookup_respects_scope(memory):
    """The join must not become a way to read another agent's private memories.

    Graph edges and memories carry scope separately, so an agent that can see
    an entity in the graph can ask for its memories. Those are still filtered.
    """
    async def go():
        mid = await memory.store(content="alice private note", type="semantic",
                                 agent_id="alice", scope="private:alice",
                                 scope_target="alice")
        await memory.anchor_entities(mid, ["Falcon"])
        return (await memory.memories_for_entities(["Falcon"], agent_id="alice"),
                await memory.memories_for_entities(["Falcon"], agent_id="bob"))
    mine, theirs = run(go())
    assert len(mine) == 1
    assert theirs == []


def test_forgetting_a_memory_removes_its_anchors(anchored):
    """Otherwise a forgotten memory stays reachable by entity, and the anchor
    row keeps a record of what it was about. That is the changelog retention
    bug again in a different table.
    """
    memory, mid = anchored
    run(memory.forget(mid))
    assert run(memory.entities_for_memories([mid])) == []
    assert run(memory.memories_for_entities(["Shortform"], agent_id="fox")) == []


def test_forgetting_leaves_no_entity_name_on_disk_for_that_memory(memory, tmp_path):
    """The anchor table names entities, which is content about the memory.

    Asserted on the raw bytes for the same reason as test_memory_forget_erases:
    a DELETE only unlinks the row, so querying cannot answer this.
    """
    canary = "ZZQQ-entity-canary-4417"

    async def go():
        mid = await memory.store(content="a memory mentioning something",
                                 type="semantic", agent_id="t")
        await memory.anchor_entities(mid, [canary])
        return mid

    def on_disk() -> list[str]:
        # The store runs in WAL mode, so a just-written row lives in the -wal
        # file and not yet in the .db. Reading only the database would report
        # the canary gone while it sits in the log.
        return [p.name for p in sorted(tmp_path.glob("memory.db*"))
                if canary.encode() in p.read_bytes()]

    mid = run(go())
    assert on_disk(), "precondition: the anchor was written"
    run(memory.forget(mid))
    assert on_disk() == []


def test_unknown_entities_return_nothing_rather_than_everything(anchored):
    memory, _ = anchored
    assert run(memory.memories_for_entities(["Nobody"], agent_id="fox")) == []
    assert run(memory.memories_for_entities([], agent_id="fox")) == []


def test_entities_are_ranked_by_how_many_hits_mention_them(memory):
    """Only the top few anchors get a traversal budget, so which ones they are
    must be a property of the results and not of SQLite's scan order.
    """
    async def go():
        ids = []
        for i in range(3):
            mid = await memory.store(content=f"note {i}", type="semantic", agent_id="t")
            await memory.anchor_entities(mid, ["Shared"] + ([f"Solo{i}"] if i else []))
            ids.append(mid)
        return await memory.entities_for_memories(ids)
    assert run(go())[0] == "Shared"
