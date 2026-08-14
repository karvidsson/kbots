"""Graph memory layer: GraphMemory store, scope isolation, tools, degradation."""

import pytest

from src.core.base import ToolContext
from src.lib import graph_store
from src.lib.graph_store import GraphMemory, GraphUnavailableError, get_graph, init_graph
from src.tools.graph import _WRITE_RE, memory_graph, memory_link, memory_related

try:
    import ladybug  # noqa: F401
    _ladybug_missing = False
except ImportError:
    _ladybug_missing = True

needs_ladybug = pytest.mark.skipif(_ladybug_missing, reason="ladybug not installed")


def _gm(tmp_path) -> GraphMemory:
    return GraphMemory({"enabled": True, "path": str(tmp_path / "g.lbdb")})


def _ctx(agent_id="alice"):
    return ToolContext(agent_id=agent_id)


# --- GraphMemory (needs the ladybug package) ---

@needs_ladybug
async def test_link_and_related(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("Alice", "works_at", "Acme", scope="global", created_by="alice")
        edges = await gm.related("Alice", agent_id="alice")
        assert len(edges) == 1
        e = edges[0]
        assert (e["src"], e["rel"], e["dst"]) == ("Alice", "works_at", "Acme")
        assert e["scope"] == "global" and e["hop"] == 1
    finally:
        gm.close()


@needs_ladybug
async def test_related_depth(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("A", "knows", "B", scope="global", created_by="alice")
        await gm.link("B", "knows", "C", scope="global", created_by="alice")
        depth1 = await gm.related("A", depth=1, agent_id="alice")
        assert {e["dst"] for e in depth1} == {"B"}
        depth2 = await gm.related("A", depth=2, agent_id="alice")
        assert {(e["src"], e["dst"]) for e in depth2} == {("A", "B"), ("B", "C")}
        # depth is capped at MAX_DEPTH, invalid values clamp instead of raising
        assert await gm.related("A", depth=99, agent_id="alice")
    finally:
        gm.close()


@needs_ladybug
async def test_scope_isolation(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("Alice", "prefers", "Coffee", scope="agent", created_by="alice")
        await gm.link("Alice", "works_at", "Acme", scope="global", created_by="alice")
        await gm.link("Alice", "member_of", "Ops", scope="group:ops", created_by="alice")
        bob_view = await gm.related("Alice", agent_id="bob")
        assert {e["rel"] for e in bob_view} == {"works_at", "member_of"}
        alice_view = await gm.related("Alice", agent_id="alice")
        assert {e["rel"] for e in alice_view} == {"prefers", "works_at", "member_of"}
    finally:
        gm.close()


@needs_ladybug
async def test_link_idempotent(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("A", "uses", "B", confidence=0.5, scope="global", created_by="alice")
        await gm.link("A", "uses", "B", confidence=0.9, scope="global", created_by="alice")
        edges = await gm.related("A", agent_id="alice")
        assert len(edges) == 1
        assert edges[0]["confidence"] == pytest.approx(0.9)
    finally:
        gm.close()


@needs_ladybug
async def test_unlink_own_edges_only(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("Alice", "prefers", "Tea", scope="agent", created_by="alice")
        assert await gm.unlink("Alice", "prefers", "Tea", agent_id="bob") == 0
        assert await gm.unlink("Alice", "prefers", "Tea", agent_id="alice") == 1
        assert await gm.related("Alice", agent_id="alice") == []
    finally:
        gm.close()


@needs_ladybug
async def test_reopen_persistence(tmp_path):
    gm = _gm(tmp_path)
    await gm.link("A", "knows", "B", scope="global", created_by="alice")
    gm.close()
    gm2 = _gm(tmp_path)
    try:
        edges = await gm2.related("A", agent_id="alice")
        assert len(edges) == 1 and edges[0]["dst"] == "B"
    finally:
        gm2.close()


@needs_ladybug
async def test_export_for_visualization(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("A", "knows", "B", scope="global", created_by="alice")
        await gm.link("B", "uses", "C", scope="agent", created_by="alice")
        data = await gm.export(agent_id="bob")
        assert {n["name"] for n in data["nodes"]} == {"A", "B"}
        assert len(data["edges"]) == 1
    finally:
        gm.close()


@needs_ladybug
async def test_export_own_only(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("A", "knows", "B", scope="global", created_by="alice")
        await gm.link("B", "uses", "C", scope="agent", created_by="alice")
        await gm.link("C", "runs", "D", scope="global", created_by="bob")
        # own_only: alice sees only what she created — not bob's global edge
        data = await gm.export(agent_id="alice", own_only=True)
        assert {(e["src"], e["dst"]) for e in data["edges"]} == {("A", "B"), ("B", "C")}
        # default (visible) view still includes bob's global edge
        data = await gm.export(agent_id="alice")
        assert len(data["edges"]) == 3
        # bob's own view: just his edge
        data = await gm.export(agent_id="bob", own_only=True)
        assert {(e["src"], e["dst"]) for e in data["edges"]} == {("C", "D")}
    finally:
        gm.close()


# --- Scope normalization (pure functions, no DB) ---

def test_normalize_scope():
    assert graph_store._normalize_scope("agent", "alice") == "agent:alice"
    assert graph_store._normalize_scope("global", None) == "global"
    assert graph_store._normalize_scope("group:ops", None) == "group:ops"
    with pytest.raises(ValueError):
        graph_store._normalize_scope("everyone", "alice")
    with pytest.raises(ValueError):
        graph_store._normalize_scope("agent", None)


# --- Read-only guard (pure regex, no DB) ---

def test_readonly_guard():
    assert _WRITE_RE.search("MERGE (a:Entity {name:'x'})")
    assert _WRITE_RE.search("MATCH (a) DETACH DELETE a")
    assert _WRITE_RE.search("create node table X(name STRING)")
    assert not _WRITE_RE.search("MATCH (a:Entity)-[r:Related]->(b) RETURN a.name, r.rel")


# --- Singleton + degradation (no ladybug needed) ---

async def test_init_graph_disabled():
    assert init_graph({"graph": {"enabled": False}}) is None
    with pytest.raises(GraphUnavailableError):
        get_graph()


async def test_tools_degrade_when_unavailable():
    graph_store.set_unavailable("Graph memory is not enabled — install kbots[graph]")
    out = await memory_link(_ctx(), "A", "knows", "B")
    assert "kbots[graph]" in out
    out = await memory_related(_ctx(), "A")
    assert "kbots[graph]" in out
    out = await memory_graph(_ctx())
    assert "kbots[graph]" in out


async def test_tool_rejects_write_cypher():
    from src.tools.graph import memory_graph_query
    out = await memory_graph_query(_ctx(), "MATCH (a) DETACH DELETE a")
    assert "Rejected" in out
