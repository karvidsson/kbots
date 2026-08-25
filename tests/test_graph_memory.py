"""Graph memory layer: GraphMemory store, scope isolation, tools, degradation."""

import pytest

from src.core.base import ToolContext
from src.lib import graph_store
from src.lib.graph_store import GraphMemory, GraphUnavailableError, get_graph, init_graph
from src.tools.graph import memory_graph, memory_link, memory_related

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
async def test_export_all_scopes(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("A", "knows", "B", scope="agent", created_by="alice")
        await gm.link("C", "runs", "D", scope="agent", created_by="bob")
        await gm.link("A", "uses", "D", scope="global", created_by="alice")
        # the fleet view sees every edge, private ones included
        data = await gm.export(all_scopes=True)
        assert len(data["edges"]) == 3
        assert {n["name"] for n in data["nodes"]} == {"A", "B", "C", "D"}
        # scope filtering still applies without the flag
        assert len((await gm.export(agent_id="carol"))["edges"]) == 1
    finally:
        gm.close()


@needs_ladybug
async def test_memory_graph_all_view_is_gated(tmp_path, monkeypatch):
    """view='all' reveals private edges — unprivileged agents must be refused."""
    from src.tools import graph as graph_tools
    gm = _gm(tmp_path)
    monkeypatch.setattr(graph_store, "_graph", gm)
    try:
        await gm.link("Alice", "prefers", "Tea", scope="agent", created_by="alice")
        monkeypatch.setattr(graph_tools, "_may_view_all", lambda agent_id: False)
        out = await memory_graph(_ctx("bob"), view="all")
        assert "privileged" in out and "Tea" not in out
        monkeypatch.setattr(graph_tools, "_may_view_all", lambda agent_id: True)
        ctx = ToolContext(agent_id="bob", project_dir=str(tmp_path))
        out = await memory_graph(ctx, title="Fleet", view="all")
        assert "memory-graph-" in out and "1 relationships" in out
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
    # An agent may name its own private scope explicitly...
    assert graph_store._normalize_scope("agent:alice", "alice") == "agent:alice"
    # ...but never another agent's (memory-poisoning guard).
    with pytest.raises(ValueError):
        graph_store._normalize_scope("agent:bob", "alice")
    with pytest.raises(ValueError):
        graph_store._normalize_scope("everyone", "alice")
    with pytest.raises(ValueError):
        graph_store._normalize_scope("agent", None)


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


@needs_ladybug
async def test_graph_query_is_scope_filtered(tmp_path):
    """memory_graph_query must never surface another agent's private edges."""
    from src.tools.graph import memory_graph_query
    gm = _gm(tmp_path)
    graph_store._graph = gm
    try:
        await gm.link("Alice", "prefers", "Tea", scope="agent", created_by="alice")
        await gm.link("Alice", "works_at", "Acme", scope="global", created_by="alice")
        # Bob queries — must see the global edge, never Alice's private one.
        out = await memory_graph_query(_ctx("bob"))
        assert "works_at" in out
        assert "prefers" not in out and "Tea" not in out
    finally:
        graph_store._graph = None
        gm.close()


def test_graph_html_escapes_rel():
    """Markup in graph data must not inject into the exported HTML JS.

    Fed to the renderer directly rather than through link(). Relations are now
    normalised to snake_case on write, which strips this payload before it
    could ever reach the renderer, but that is a second line of defence and
    testing through it would stop testing the escaping at all. Entity names
    are not normalised, so this remains a reachable path.
    """
    from src.tools.graph import _render_graph_html
    nodes = [{"name": "</script><img src=x onerror=alert(1)>", "type": "e", "degree": 1},
             {"name": "B", "type": "e", "degree": 1}]
    edges = [{"src": nodes[0]["name"], "rel": "</script>evil", "dst": "B",
              "confidence": 0.9, "scope": "global", "created_by": "alice"}]
    html = _render_graph_html(nodes, edges, "T")
    # The injected closing tag must be escaped in the embedded JSON payload,
    # leaving only the document's own real </script> tags (D3 loader + app).
    assert "<\\/script>" in html
    assert html.count("</script>") == 2
    assert "onerror=alert(1)>" not in html.split("<script>")[0]


@needs_ladybug
async def test_link_strips_markup_from_relations(tmp_path):
    """Normalisation is also a sanitiser: a relation cannot carry markup."""
    gm = _gm(tmp_path)
    try:
        await gm.link("A", "</script><img src=x onerror=alert(1)>", "B",
                      scope="global", created_by="alice")
        edges = await gm.related("A", agent_id="alice")
        assert "<" not in edges[0]["rel"] and ">" not in edges[0]["rel"]
    finally:
        gm.close()


# --- Loopback routing: GraphClient → InternalAPI /graph → GraphMemory ---
# The path agents actually use: tools run in the MCP subprocess and forward
# every graph call to the main process. A stub-only test here is what let the
# "structurally unreachable" bug ship — keep this one end-to-end.

async def _start_api():
    from src.core.internal_api import InternalAPI
    api = InternalAPI(object(), {"port": 0})
    await api.start()
    return api


@needs_ladybug
async def test_graph_client_loopback_end_to_end(tmp_path, monkeypatch):
    from src.lib.graph_store import GraphClient
    gm = _gm(tmp_path)
    monkeypatch.setattr(graph_store, "_graph", gm)
    api = await _start_api()
    try:
        client = GraphClient(f"http://{api.host}:{api.port}", api.token)
        edge = await client.link("Alice", "works_at", "Acme",
                                 scope="agent", created_by="alice")
        assert edge["scope"] == "agent:alice"
        edges = await client.related("Alice", agent_id="alice")
        assert [(e["src"], e["dst"]) for e in edges] == [("Alice", "Acme")]
        data = await client.export(agent_id="alice", own_only=True)
        assert len(data["edges"]) == 1
        rows = await client.find(rel="works_at", agent_id="alice")
        assert [(r["src"], r["dst"]) for r in rows] == [("Alice", "Acme")]
        assert await client.unlink("Alice", "works_at", "Acme", agent_id="alice") == 1
        # a bad scope surfaces as ValueError across the process boundary,
        # matching in-process GraphMemory behavior (memory_link catches it)
        with pytest.raises(ValueError):
            await client.link("A", "r", "B", scope="bogus")
    finally:
        await api.stop()
        gm.close()


async def test_graph_client_auth_and_disabled(monkeypatch):
    from src.lib.graph_store import GraphClient
    monkeypatch.setattr(graph_store, "_graph", None)
    api = await _start_api()
    try:
        # graph disabled in the main process → unavailable, not a crash
        client = GraphClient(f"http://{api.host}:{api.port}", api.token)
        with pytest.raises(GraphUnavailableError):
            await client.related("Alice")
        # wrong bearer token → unavailable, never dispatched
        bad = GraphClient(f"http://{api.host}:{api.port}", "wrong-token")
        with pytest.raises(GraphUnavailableError):
            await bad.related("Alice")
    finally:
        await api.stop()


def test_init_graph_client_requires_loopback_env(monkeypatch):
    monkeypatch.delenv("KBOTS_INTERNAL_API", raising=False)
    monkeypatch.delenv("KBOTS_INTERNAL_TOKEN", raising=False)
    monkeypatch.setattr(graph_store, "_graph", None)
    assert graph_store.init_graph_client() is False
    monkeypatch.setenv("KBOTS_INTERNAL_API", "http://127.0.0.1:1")
    monkeypatch.setenv("KBOTS_INTERNAL_TOKEN", "t")
    assert graph_store.init_graph_client() is True
    assert isinstance(graph_store._graph, graph_store.GraphClient)
