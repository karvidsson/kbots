"""Fused recall: one pipeline, every engine, every turn.

What this replaces: `_auto_recall` ran a single FTS5 keyword search and nothing
else. Embeddings were computed for every stored memory and never consulted at
recall time, and the graph the reflector spends an LLM call per batch building
was never traversed by anything. Whether a memory surfaced depended on whether
the agent happened to call the right tool by hand.

The pipeline here is fixed rather than routed. Keyword and vector search always
run; entity anchors on the results always open the graph; the graph's
neighbours always pull their own memories back. Results are fused by rank, not
by score, because FTS5 `rank` and cosine similarity are not comparable numbers.

A fixed pipeline is also the only version that can be tested: there is one path
through it, so a golden-query set measures the system rather than measuring how
a particular question happened to be worded.
"""

import asyncio
import logging

from src.memory.query import rrf_merge

logger = logging.getLogger(__name__)

# How many candidates each engine contributes before fusion. Deliberately
# larger than the final limit: fusion is only useful if the engines are allowed
# to disagree about what belongs in the top few.
CANDIDATES = 12

# Entities to expand from the top hits. One hop is the whole budget. Two hops
# on a graph this dense pulls in most of the fleet and stops being evidence.
MAX_ANCHORS = 6
GRAPH_HOPS = 1

# How many hits from EACH engine may contribute entity anchors.
ANCHOR_SEEDS = 8

# Slots in the final bundle reserved for memories only the graph found.
#
# Rank fusion alone silently deletes the graph. A graph-only result is in one
# ranked list, so it scores 1/(k+1) at best, while anything two engines agree
# on scores more than that from any position: two twelfth places beat one
# first. The graph hop is precisely the engine whose results do NOT look like
# the query, so it is always the minority vote and never survives.
#
# Reserving slots rather than weighting the list keeps fusion honest about the
# engines that are comparable and makes the trade explicit: at most this many
# of `limit` are given to results the other engines did not rank.
GRAPH_SLOTS = 2


async def _safe(coro, what: str):
    """Run one engine; a failure degrades the bundle instead of emptying it."""
    try:
        return await coro
    except Exception as e:
        logger.debug(f"recall: {what} failed: {e}")
        return []


async def recall(memory, text: str, agent_id: str | None = None,
                 limit: int = 5, graph=None, type: str | None = None,
                 category: str | None = None) -> list[dict]:
    """Return the fused, ranked memories for `text`.

    Each result carries `sources`, naming the engines that found it, so a
    caller can show why something is in the bundle. Graph-derived results are
    marked with the entity they were reached through.

    `type` and `category` narrow the result set. They are pushed down into the
    keyword engine and applied again after fusion, because the vector and graph
    engines have no filter of their own: a filter honoured by one engine and
    ignored by two is worse than no filter, since the caller cannot tell which
    results were checked.
    """
    if not text:
        return []

    filters = {k: v for k, v in (("type", type), ("category", category)) if v}

    keyword_task = _safe(
        memory.search(query=text, agent_id=agent_id, limit=CANDIDATES, **filters),
        "keyword")
    if hasattr(memory, "semantic_search"):
        vector_task = _safe(
            memory.semantic_search(query=text, agent_id=agent_id, limit=CANDIDATES),
            "semantic")
    else:
        vector_task = _safe(asyncio.sleep(0, result=[]), "semantic")

    keyword, vector = await asyncio.gather(keyword_task, vector_task)

    ranked = [("keyword", keyword), ("semantic", vector)]

    graph_hits = await _graph_expand(memory, graph, keyword, vector, agent_id)
    if graph_hits:
        ranked.append(("graph", graph_hits))

    fused = rrf_merge(ranked)
    if filters:
        fused = [m for m in fused
                 if all(m.get(k) == v for k, v in filters.items())]
    return _reserve_graph_slots(fused, limit)


def _reserve_graph_slots(fused: list[dict], limit: int) -> list[dict]:
    """Keep the best graph-only results in the bundle, then fill by rank.

    Order within the bundle is still the fused order; the reservation decides
    membership, not ranking, so a graph result does not get to claim it was the
    best answer.
    """
    head = fused[:limit]
    if len(fused) <= limit or any(m.get("via_graph") for m in head):
        return head
    # Never more than a quarter of the bundle. A flat two slots is a third of a
    # top-5 and two thirds of a top-3: measured on the golden set, that evicted
    # a correct vector answer to make room for a neighbour, and fused recall
    # scored below vector search alone. The reservation is insurance against
    # the graph being silently deleted by fusion, not a claim that graph
    # neighbours are usually the answer.
    slots = max(1, min(GRAPH_SLOTS, limit // 4))
    graph_only = [m for m in fused if m.get("via_graph")][:slots]
    if not graph_only:
        return head
    keep = head[:max(0, limit - len(graph_only))] + graph_only
    ordering = {id(m): i for i, m in enumerate(fused)}
    return sorted(keep, key=lambda m: ordering[id(m)])


async def _graph_expand(memory, graph, keyword, vector, agent_id) -> list[dict]:
    """Memories reached by walking one hop out from what search already found.

    Search finds documents that look like the question. This finds documents
    connected to what the question is about, which is the thing a vector index
    cannot do: nothing in the text of "the deploy broke" resembles the text of
    "self-deploy.sh runs the test gate", but they are one edge apart.
    """
    if graph is None or not hasattr(memory, "memories_for_entities"):
        return []

    # Seeds come from BOTH engines, evenly. Concatenating and truncating to
    # CANDIDATES silently took the keyword list and nothing else, which killed
    # the graph hop on exactly the queries keyword search is worst at, and
    # those are the queries the graph hop exists for.
    seed_ids: list[str] = []
    for engine in (keyword, vector):
        for m in list(engine)[:ANCHOR_SEEDS]:
            if m.get("id") and m["id"] not in seed_ids:
                seed_ids.append(m["id"])
    if not seed_ids:
        return []

    anchors = await _safe(memory.entities_for_memories(seed_ids), "anchors")
    if not anchors:
        return []
    anchors = anchors[:MAX_ANCHORS]

    neighbours: list[str] = []
    for entity in anchors:
        edges = await _safe(
            graph.related(entity, depth=GRAPH_HOPS, agent_id=agent_id, limit=25),
            f"traverse({entity})")
        for e in edges or []:
            for node in (e.get("src"), e.get("dst")):
                if node and node not in anchors and node not in neighbours:
                    neighbours.append(node)
    if not neighbours:
        return []

    hits = await _safe(
        memory.memories_for_entities(neighbours, agent_id=agent_id,
                                     limit=CANDIDATES), "graph memories")
    # Excluded against EVERY search result, not just the seeds that produced
    # the anchors. A memory ranked below the seed window would otherwise come
    # back through the graph and collect a second vote, and rank fusion reads
    # that as two independent engines agreeing when it is one engine counted
    # twice. Measured on the golden set, that promoted an unrelated memory to
    # first place and pushed the correct answer out of the top three.
    #
    # It also states what the hop is for: memories search did not find.
    seen = {m.get("id") for m in list(keyword) + list(vector)}
    out = []
    for h in hits or []:
        if h.get("id") in seen:
            continue
        h["via_graph"] = True
        out.append(h)
    return out


def format_block(results: list[dict], agent_id: str | None = None) -> str:
    """Render recalled memories as the context block agents receive.

    Another agent's memory is labelled with who wrote it. Unattributed, a
    lesson learned by the music agent about its own distributor reads as
    something this agent knows, and the fleet-wide read that makes knowledge
    compound would also make every agent confidently wrong about six other
    domains.
    """
    if not results:
        return ""
    lines = ["<auto-recalled-memories>"]
    for mem in results:
        content = (mem.get("content") or "").strip()
        if not content:
            continue
        category = mem.get("category") or "general"
        via = " via graph" if mem.get("via_graph") else ""
        author = mem.get("created_by")
        by = f" learned by {author}" if author and author != agent_id else ""
        lines.append(f"[{category}{via}{by}] {content}")
    lines.append("</auto-recalled-memories>")
    return "\n".join(lines) if len(lines) > 2 else ""
