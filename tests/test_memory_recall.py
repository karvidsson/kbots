"""Fused recall: keyword, vector and one graph hop, on every turn.

What this replaces (2026-08-22): `_auto_recall` ran a single keyword search.
Embeddings were computed for every memory and never consulted at recall time,
and the graph the reflector spends an LLM call per batch building was never
traversed by anything. Which engine ran depended on which tool an agent
happened to reach for, so recall quality was a property of the model's mood.

The pipeline is fixed rather than routed, and these tests exist because a fixed
pipeline is the only version that can be tested at all: there is one path
through it, so a failure is a failure of the system rather than of a
particular phrasing.
"""

import asyncio

import pytest

from src.memory.recall import format_block, recall


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def seeded(memory):
    """A store with memories that each engine alone would find differently."""
    async def go():
        ids = {}
        ids["keyword"] = await memory.store(
            content="The supervised debug Chrome listens on port 9222.",
            type="semantic", agent_id="eng", scope="global")
        ids["vector"] = await memory.store(
            content="chrome debug port listens supervised",
            type="semantic", agent_id="eng", scope="global")
        ids["unrelated"] = await memory.store(
            content="Invoices are reconciled from the bank CSV export.",
            type="semantic", agent_id="eng", scope="global")
        return ids
    return memory, run(go())


def test_recall_runs_both_engines_and_labels_who_found_what(seeded):
    memory, ids = seeded
    results = run(recall(memory, "which port does the debug chrome listen on?",
                         agent_id="eng", limit=5))
    assert results, "fused recall returned nothing for a question about stored content"
    found = {r["id"]: r["sources"] for r in results}
    assert ids["keyword"] in found
    engines = {s for sources in found.values() for s in sources}
    assert engines >= {"keyword", "semantic"}, (
        f"an engine did not contribute: {engines}")


def test_a_question_that_used_to_return_nothing_now_returns_something(seeded):
    """The exact failure: punctuation made the whole query a syntax error.

    Before the fix this question reached FTS5 verbatim, raised
    OperationalError, fell through to `LIKE '%<the whole question>%'` and
    returned zero rows.
    """
    memory, ids = seeded
    results = run(recall(memory, "chrome won't stay up on :9222 -- what's wrong?",
                         agent_id="eng"))
    assert [r["id"] for r in results], "the punctuation case is still empty"


def test_one_engine_failing_degrades_the_bundle_instead_of_emptying_it(seeded):
    """A broken vector index must not cost the keyword answer too."""
    memory, ids = seeded

    async def boom(*a, **k):
        raise RuntimeError("embedding backend down")

    memory.semantic_search = boom
    results = run(recall(memory, "debug chrome port 9222", agent_id="eng"))
    assert results, "a vector failure emptied the whole bundle"
    assert all("semantic" not in r["sources"] for r in results)


def test_scope_is_still_enforced_through_the_fused_path(memory):
    """Fusion must not become a way around the scope filter.

    Three engines each apply scope themselves; the risk is a future engine that
    forgets, so this asserts on the fused output rather than on any one query.
    """
    async def go():
        await memory.store(content="alice private note about falcons", type="semantic",
                           agent_id="alice", scope="agent", scope_target="alice")
        return await recall(memory, "falcons note", agent_id="bob")
    assert run(go()) == []


def test_filters_are_applied_to_every_engine_not_just_the_one_that_supports_them(memory):
    """type/category are pushed down to keyword and re-applied after fusion.

    The vector engine has no filter of its own. Without the post-fusion pass a
    filtered search would return unfiltered vector hits, which is worse than
    not offering the filter: the caller believes the result set was checked.
    """
    async def go():
        await memory.store(content="deploy ritual runs the test gate", type="procedural",
                           agent_id="eng", category="ops", scope="global")
        await memory.store(content="deploy ritual anecdote from tuesday", type="episodic",
                           agent_id="eng", category="stories", scope="global")
        return await recall(memory, "deploy ritual", agent_id="eng", type="procedural")
    results = run(go())
    assert results
    assert {r["type"] for r in results} == {"procedural"}


def test_an_empty_query_recalls_nothing_rather_than_everything(memory):
    assert run(recall(memory, "", agent_id="eng")) == []


def test_limit_is_honoured_after_fusion_not_before(memory):
    """Each engine contributes more candidates than the caller asked for.

    Trimming to `limit` inside each engine would make fusion pointless: the
    engines could never disagree about anything outside the top few, which is
    where agreement carries information.
    """
    async def go():
        for i in range(12):
            await memory.store(content=f"deploy note number {i} about the gate",
                               type="semantic", agent_id="eng", scope="global")
        return await recall(memory, "deploy note gate", agent_id="eng", limit=3)
    results = run(go())
    assert len(results) == 3


# --- the graph hop ---

class FakeGraph:
    """One edge: whatever is asked for is related to 'self-deploy.sh'."""

    def __init__(self, edges):
        self.edges = edges
        self.asked = []

    async def related(self, entity, depth=1, agent_id=None, limit=25):
        self.asked.append(entity)
        return [e for e in self.edges
                if entity in (e["src"], e["dst"])]


def test_the_graph_hop_finds_a_memory_that_shares_no_words_with_the_query(memory):
    """The one thing neither keyword nor vector search can do.

    Nothing in the text of "why did the deploy break" resembles "the gate runs
    ruff and pytest", but they are one edge apart through the entity
    'self-deploy.sh'. This is what the graph was being built for and what
    nothing was reading.
    """
    async def go():
        seed = await memory.store(content="The deploy broke on tuesday.",
                                  type="episodic", agent_id="eng", scope="global")
        neighbour = await memory.store(
            content="Ruff and pytest both run before anything restarts.",
            type="semantic", agent_id="eng", scope="global")
        # Enough closer matches to fill both engines' candidate slots, so the
        # neighbour can only arrive through the graph. Without this the corpus
        # is smaller than the candidate window and every engine returns
        # everything, which would make the test pass for the wrong reason.
        for i in range(20):
            await memory.store(content=f"deploy note {i}: the deploy broke again",
                               type="episodic", agent_id="eng", scope="global")
        await memory.anchor_entities(seed, ["deploy"])
        await memory.anchor_entities(neighbour, ["self-deploy.sh"])
        graph = FakeGraph([{"src": "deploy", "rel": "uses", "dst": "self-deploy.sh"}])
        results = await recall(memory, "the deploy broke", agent_id="eng",
                               limit=5, graph=graph)
        return neighbour, results

    neighbour, results = run(go())
    hit = [r for r in results if r["id"] == neighbour]
    assert hit, "the graph neighbour was not recalled"
    assert "graph" in hit[0]["sources"]
    assert hit[0].get("via_graph") is True


def test_anchors_come_from_the_vector_engine_too_not_only_from_keyword(memory):
    """Regression: seeds were concatenated and then truncated.

    keyword + vector sliced to the candidate width is just the keyword list, so
    the graph hop was dead on every query keyword search missed. Those are
    precisely the queries the graph hop exists to answer.

    Here the seed memory is unfindable by keyword (it shares no word with the
    query) and findable by vector, so the anchor can only come from the vector
    engine.
    """
    async def go():
        seed = await memory.store(content="restarts pytest ruff gate",
                                  type="semantic", agent_id="eng", scope="global")
        neighbour = await memory.store(content="Kristian prefers short reports.",
                                       type="semantic", agent_id="eng", scope="global")
        for i in range(20):
            await memory.store(content=f"unrelated filler memory {i} about invoices",
                               type="semantic", agent_id="eng", scope="global")
        await memory.anchor_entities(seed, ["self-deploy.sh"])
        await memory.anchor_entities(neighbour, ["Kristian"])
        graph = FakeGraph([{"src": "self-deploy.sh", "rel": "owned_by", "dst": "Kristian"}])
        results = await recall(memory, "ruff pytest gate restarts", agent_id="eng",
                               limit=5, graph=graph)
        return neighbour, graph, results

    neighbour, graph, results = run(go())
    assert graph.asked, "the graph was never traversed"
    assert neighbour in [r["id"] for r in results]


def test_a_graph_only_result_keeps_a_slot_against_the_fused_majority(memory):
    """Rank fusion alone deletes the graph, quietly.

    A graph-only hit is in one ranked list, so it scores 1/(k+1) at best, while
    anything two engines agree on scores more than that from any position: two
    twelfth places beat one first. Since graph results are the ones that do NOT
    look like the query, they are always the minority vote. Without a reserved
    slot the hop runs, costs a traversal, and never reaches the agent.
    """
    async def go():
        seed = await memory.store(content="the deploy broke on tuesday",
                                  type="episodic", agent_id="eng", scope="global")
        neighbour = await memory.store(content="unrelated wording entirely",
                                       type="semantic", agent_id="eng", scope="global")
        # Twenty memories that both engines rank, so every fused slot has a
        # two-engine candidate competing for it.
        for i in range(20):
            await memory.store(content=f"the deploy broke on tuesday variant {i}",
                               type="episodic", agent_id="eng", scope="global")
        await memory.anchor_entities(seed, ["deploy"])
        await memory.anchor_entities(neighbour, ["self-deploy.sh"])
        graph = FakeGraph([{"src": "deploy", "rel": "uses", "dst": "self-deploy.sh"}])
        return neighbour, await recall(memory, "the deploy broke on tuesday",
                                       agent_id="eng", limit=5, graph=graph)

    neighbour, results = run(go())
    assert neighbour in [r["id"] for r in results]
    # The reservation decides membership, not ranking: it must not claim to be
    # the best answer.
    assert results[0]["id"] != neighbour


def test_reserved_graph_slots_never_take_over_the_bundle(memory):
    async def go():
        seed = await memory.store(content="the deploy broke on tuesday",
                                  type="episodic", agent_id="eng", scope="global")
        await memory.anchor_entities(seed, ["deploy"])
        for i in range(8):
            mid = await memory.store(content=f"graph neighbour {i} worded differently",
                                     type="semantic", agent_id="eng", scope="global")
            await memory.anchor_entities(mid, ["self-deploy.sh"])
        for i in range(20):
            await memory.store(content=f"the deploy broke on tuesday variant {i}",
                               type="episodic", agent_id="eng", scope="global")
        graph = FakeGraph([{"src": "deploy", "rel": "uses", "dst": "self-deploy.sh"}])
        return await recall(memory, "the deploy broke on tuesday", agent_id="eng",
                            limit=5, graph=graph)

    results = run(go())
    from src.memory.recall import GRAPH_SLOTS
    assert sum(1 for r in results if r.get("via_graph")) <= GRAPH_SLOTS


def test_a_missing_graph_costs_the_hop_and_nothing_else(seeded):
    memory, ids = seeded
    results = run(recall(memory, "debug chrome port", agent_id="eng", graph=None))
    assert results


def test_a_broken_graph_costs_the_hop_and_nothing_else(memory):
    class Exploding:
        async def related(self, *a, **k):
            raise RuntimeError("graph store closed")

    async def go():
        mid = await memory.store(content="chrome debug port 9222", type="semantic",
                                 agent_id="eng", scope="global")
        await memory.anchor_entities(mid, ["chrome"])
        return await memory.__class__.search(memory, "chrome", "eng"), await recall(
            memory, "chrome debug port", agent_id="eng", graph=Exploding())
    _, results = run(go())
    assert results, "a graph failure emptied the bundle"


def test_the_graph_never_gives_a_second_vote_to_a_memory_search_already_found(memory):
    """Regression: graph hits were deduped against the anchor seeds only.

    A memory ranked below the seed window came back through the graph and
    collected a second vote, and rank fusion reads that as two independent
    engines agreeing when it is one engine counted twice. On the golden set it
    promoted an unrelated memory to first place and pushed the correct answer
    out of the top three.
    """
    async def go():
        seed = await memory.store(content="chrome debug port 9222", type="semantic",
                                  agent_id="eng", scope="global")
        await memory.anchor_entities(seed, ["Chrome"])
        # Ranked by the engines, but far enough down to fall outside the seed
        # window that produces anchors.
        tail = []
        for i in range(12):
            mid = await memory.store(content=f"chrome note {i} about the browser",
                                     type="semantic", agent_id="eng", scope="global")
            await memory.anchor_entities(mid, ["Browser"])
            tail.append(mid)
        graph = FakeGraph([{"src": "Chrome", "rel": "uses", "dst": "Browser"}])
        return tail, await recall(memory, "chrome debug port browser", agent_id="eng",
                                  limit=10, graph=graph)

    tail, results = run(go())
    for r in results:
        if r["id"] in tail:
            assert "graph" not in r["sources"], (
                "a memory the search engines already found was counted again "
                "as a graph discovery")


def test_graph_results_never_duplicate_what_search_already_found(memory):
    async def go():
        mid = await memory.store(content="chrome debug port 9222 supervised",
                                 type="semantic", agent_id="eng", scope="global")
        await memory.anchor_entities(mid, ["chrome"])
        graph = FakeGraph([{"src": "chrome", "rel": "uses", "dst": "chrome"}])
        return mid, await recall(memory, "chrome debug port", agent_id="eng",
                                 graph=graph)
    mid, results = run(go())
    assert [r["id"] for r in results].count(mid) == 1


# --- the context block agents actually see ---

def test_format_block_marks_graph_results_so_the_agent_knows_why_they_are_there():
    block = format_block([
        {"content": "a fact", "category": "general"},
        {"content": "a neighbour", "category": "project", "via_graph": True},
    ])
    assert "<auto-recalled-memories>" in block and "</auto-recalled-memories>" in block
    assert "[general] a fact" in block
    assert "[project via graph] a neighbour" in block


def test_format_block_is_empty_when_there_is_nothing_to_say():
    """An empty wrapper is worse than no block: it reads as 'nothing is known'."""
    assert format_block([]) == ""
    assert format_block([{"content": "   "}]) == ""
