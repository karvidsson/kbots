"""Memory tools — direct SQLite access via the inline memory backend.

Replaces the HTTP wrappers that called memory-api (:8897).
All operations go through ctx.memory (SQLiteMemory instance).
"""

import logging

from src.core.base import ToolContext
from src.core.tools import tool
from src.memory.recall import recall

logger = logging.getLogger(__name__)


def _get_memory(ctx: ToolContext):
    """Get memory backend from context, raise clear error if missing."""
    if ctx.memory is None:
        raise RuntimeError("Memory backend not configured for this agent")
    return ctx.memory


@tool(name="memory_store", description="Store information in long-term memory", category="memory")
async def memory_store(
    ctx: ToolContext,
    content: str,
    type: str = "semantic",
    category: str = "general",
    tags: str = "",
    scope: str = "agent",
) -> str:
    """Store a piece of information in persistent memory.

    Args:
        content: The information to remember
        type: Memory type — semantic (facts/knowledge), episodic (events/experiences), procedural (how-to/processes)
        category: Category (general, project, people, business, etc.)
        tags: Comma-separated tags
        scope: Visibility.
            agent (default) — filed under you, and readable by the rest of the
                fleet. Use it for anything you learned that another agent could
                act on: a tool's real behaviour, a gotcha, a fact about the
                owner or the business.
            private — yours alone, never shared. For working notes that would
                only be noise to anyone else, or anything about an account or a
                person that is not the fleet's business.
            global — the same visibility as agent, stated deliberately. Use it
                for established facts about the world, the business or the team.
            group:<name> — members of that group only.
    """
    # Map friendly names to DB-valid types
    type_map = {"fact": "semantic", "preference": "semantic", "episode": "episodic",
                "procedure": "procedural", "process": "procedural"}
    requested = type
    type = type_map.get(type, type)
    # An unknown type used to become 'semantic' in silence, so a caller using
    # the file-memory vocabulary ('reference', 'feedback', 'user', 'project')
    # got a memory filed under a type it never asked for and no way to notice.
    coerced = type not in ("semantic", "episodic", "procedural")
    if coerced:
        type = "semantic"
    if scope not in ("agent", "global", "private") and not scope.startswith("group:"):
        return (f"Invalid scope {scope!r} — use 'agent' (yours, fleet-readable), "
                "'private' (yours alone), 'global', or 'group:<name>'.")
    memory = _get_memory(ctx)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    memory_id = await memory.store(
        content=content,
        type=type,
        agent_id=ctx.agent_id,
        tags=tag_list,
        category=category,
        scope=scope,
        scope_target=ctx.agent_id if scope in ("agent", "private") else None,
    )

    note = ""
    if coerced:
        note = (f"\nNote: type '{requested}' is not a valid memory type — stored as "
                f"'semantic'. Valid types: semantic, episodic, procedural.")
    return f"Stored memory (id: {memory_id}, scope: {scope}): {content[:80]}...{note}"


@tool(name="memory_search", description="Search long-term memory", category="memory")
async def memory_search(
    ctx: ToolContext,
    query: str,
    limit: int = 5,
    type: str = "",
    category: str = "",
) -> str:
    """Search persistent memory for relevant information.

    Runs keyword search, vector search and a one-hop graph expansion together
    and fuses the three by rank. You do not need to pick a search strategy or
    try a second tool when this one comes back empty: an empty answer here
    means every engine was empty, not that you chose the wrong door.

    Args:
        query: What to search for. A plain question works; it is turned into a
            keyword expression for you.
        limit: Max results to return
        type: Filter by memory type
        category: Filter by category
    """
    memory = _get_memory(ctx)

    # A graph that will not open costs the graph hop, not the search.
    try:
        from src.lib.graph_store import get_graph
        graph = get_graph()
    except Exception:
        graph = None

    memories = await recall(
        memory, query, agent_id=ctx.agent_id, limit=limit, graph=graph,
        type=type or None, category=category or None,
    )

    if not memories:
        return f"No memories found for: {query}"

    lines = [f"Found {len(memories)} memories for '{query}':"]
    for i, mem in enumerate(memories, 1):
        content = mem.get("content", "")
        mem_type = mem.get("type", "")
        created = mem.get("created_at", "")
        # Naming the engines is not decoration: "graph" means this memory does
        # not resemble the query and was reached through a shared entity, which
        # changes how much the reader should trust it as an answer.
        via = ", ".join(mem.get("sources") or []) or "keyword"
        author = mem.get("created_by")
        by = f" learned by {author}" if author and author != ctx.agent_id else ""
        lines.append(f"  {i}. [{mem_type}{by}] {content[:200]}")
        lines.append(f"     (id: {mem.get('id')}, found by: {via}"
                     + (f", stored: {created}" if created else "") + ")")
    return "\n".join(lines)


@tool(
    name="memory_semantic_search",
    description="Search memory using semantic similarity",
    category="memory",
)
async def memory_semantic_search(
    ctx: ToolContext,
    query: str,
    limit: int = 5,
) -> str:
    """Search memory using embedding-based semantic similarity only.

    memory_search already includes this engine and fuses it with the others,
    so reach for it only when you specifically want similarity alone, with no
    keyword or graph influence on the ranking.
    """
    memory = _get_memory(ctx)

    memories = await memory.semantic_search(
        query=query,
        agent_id=ctx.agent_id,
        limit=limit,
    )

    if not memories:
        return f"No semantically similar memories found for: {query}"

    lines = [f"Found {len(memories)} similar memories:"]
    for i, mem in enumerate(memories, 1):
        content = mem.get("content", "")
        score = mem.get("similarity", "")
        lines.append(f"  {i}. {content[:200]}")
        if score:
            lines.append(f"     (similarity: {score:.2f})")
    return "\n".join(lines)


@tool(name="memory_forget", description="Remove a memory by ID", category="memory")
async def memory_forget(ctx: ToolContext, memory_id: str) -> str:
    """Remove a specific memory from the database.

    Args:
        memory_id: The UUID returned by memory_store or shown by memory_search.
    """
    # memories.id is a TEXT column holding UUIDs, and memory_store returns one.
    # Declaring this int made the tool impossible to call at all: no value both
    # passed schema validation and matched a row, so no memory could ever be
    # deleted through the documented path. The backend accepts either form.
    memory = _get_memory(ctx)
    await memory.forget(memory_id)
    return f"Memory {memory_id} removed."
