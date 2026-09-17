"""Provenance on graph edges: which memory an edge came from.

The reflector knew the source memory id of every extracted edge and dropped it
at the write, because link() had nowhere to put it. The three cases that
matter beyond "the id is stored":

- a superseded edge keeps its own sources and the new edge does not inherit
  them, or old memories are credited with a claim they never made;
- an edge is shared across scopes, so its sources can name a memory the viewer
  may not read, and the rendered graph must count those, never show them;
- the MCP subprocess reaches the store through GraphClient, which drops any
  keyword it does not forward.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from src.core.base import ToolContext
from src.lib import graph_store
from src.lib.graph_store import (
    INFERRED_PREFIX,
    MAX_SOURCES,
    GraphMemory,
    merge_sources,
    parse_sources,
)
from src.tools.graph import memory_graph, memory_link

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory-backfill.py"
_spec = importlib.util.spec_from_file_location("memory_backfill_prov", SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

try:
    import ladybug  # noqa: F401
    _missing = False
except ImportError:
    _missing = True

needs_ladybug = pytest.mark.skipif(_missing, reason="ladybug not installed")


def _gm(tmp_path) -> GraphMemory:
    return GraphMemory({"enabled": True, "path": str(tmp_path / "g.lbdb")})


def _embedded_edges(html: str) -> list[dict]:
    m = re.search(r"const NODES=(.*?), EDGES=(.*?);\n", html)
    return json.loads(m.group(2))


async def _render(ctx, tmp_path, **kw) -> list[dict]:
    out = await memory_graph(ctx, **kw)
    path = re.search(r"generated: (\S+)", out).group(1)
    return _embedded_edges(Path(path).read_text())


# --- pure helpers ---

def test_merge_appends_without_duplicating():
    raw = merge_sources(None, "m1")
    raw = merge_sources(raw, "m2")
    raw = merge_sources(raw, "m1")
    assert parse_sources(raw) == ["m1", "m2"]
    assert merge_sources(None, None) is None


def test_merge_is_capped():
    raw = None
    for i in range(MAX_SOURCES + 5):
        raw = merge_sources(raw, f"m{i}")
    assert len(parse_sources(raw)) == MAX_SOURCES


def test_unreadable_sources_read_as_none():
    assert parse_sources("not json") == []
    assert parse_sources('{"a": 1}') == []
    assert parse_sources("") == []


# --- store ---

@needs_ladybug
async def test_link_accumulates_sources_and_reads_return_them(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("kbots", "uses", "LadybugDB", scope="global", created_by="a", source="m1")
        await gm.link("kbots", "uses", "LadybugDB", scope="global", created_by="a", source="m2")
        edge = await gm.link("kbots", "uses", "LadybugDB", scope="global",
                             created_by="a", source="m1")
        assert edge["sources"] == ["m1", "m2"]
        assert (await gm.export(agent_id="a"))["edges"][0]["sources"] == ["m1", "m2"]
        assert (await gm.find(entity="kbots", agent_id="a"))[0]["sources"] == ["m1", "m2"]
        assert (await gm.related("kbots", agent_id="a"))[0]["sources"] == ["m1", "m2"]
        assert (await gm.history("kbots", agent_id="a"))[0]["sources"] == ["m1", "m2"]
    finally:
        gm.close()


@needs_ladybug
async def test_link_without_source_keeps_existing_sources(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("A", "uses", "B", scope="global", created_by="a", source="m1")
        edge = await gm.link("A", "uses", "B", scope="global", created_by="a")
        assert edge["sources"] == ["m1"]
        other = await gm.link("C", "uses", "D", scope="global", created_by="a")
        assert other["sources"] == []
    finally:
        gm.close()


@needs_ladybug
async def test_supersession_does_not_carry_sources_to_the_new_edge(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("Kristian", "works_at", "OldCo", scope="global",
                      created_by="a", source="m-old")
        new = await gm.link("Kristian", "works_at", "NewCo", scope="global",
                            created_by="a", source="m-new")
        assert new.get("superseded") == 1
        assert new["sources"] == ["m-new"]
        by_dst = {e["dst"]: e for e in await gm.history("Kristian", agent_id="a")}
        assert by_dst["OldCo"]["sources"] == ["m-old"]
        assert by_dst["OldCo"]["current"] is False
        assert by_dst["NewCo"]["sources"] == ["m-new"]
    finally:
        gm.close()


@needs_ladybug
async def test_concurrent_links_lose_no_source(tmp_path):
    import asyncio
    gm = _gm(tmp_path)
    try:
        await gm.link("A", "uses", "B", scope="global", created_by="a", source="m0")
        await asyncio.gather(*(
            gm.link("A", "uses", "B", scope="global", created_by="a", source=f"m{i}")
            for i in range(1, 9)))
        edge = (await gm.export(agent_id="a"))["edges"][0]
        assert sorted(edge["sources"]) == sorted(f"m{i}" for i in range(9))
    finally:
        gm.close()


@needs_ladybug
async def test_graph_client_forwards_source(tmp_path, monkeypatch):
    from src.core.internal_api import InternalAPI
    from src.lib.graph_store import GraphClient
    gm = _gm(tmp_path)
    monkeypatch.setattr(graph_store, "_graph", gm)
    api = InternalAPI(object(), {"port": 0})
    await api.start()
    try:
        client = GraphClient(f"http://{api.host}:{api.port}", api.token)
        edge = await client.link("Alice", "works_at", "Acme", scope="agent",
                                 created_by="alice", source="m1")
        assert edge["sources"] == ["m1"]
        data = await client.export(agent_id="alice", own_only=True)
        assert data["edges"][0]["sources"] == ["m1"]
    finally:
        await api.stop()
        gm.close()


# --- tool ---

@needs_ladybug
async def test_memory_link_records_a_real_source_and_refuses_an_unknown_one(
        tmp_path, monkeypatch, memory):
    gm = _gm(tmp_path)
    monkeypatch.setattr(graph_store, "_graph", gm)
    try:
        mid = await memory.store(content="Alice works at Acme.", type="semantic",
                                 agent_id="alice", scope="global")
        ctx = ToolContext(agent_id="alice", memory=memory)
        out = await memory_link(ctx, "Alice", "works_at", "Acme", source="nope")
        assert "No memory" in out
        assert (await gm.export(agent_id="alice"))["edges"] == []
        await memory_link(ctx, "Alice", "works_at", "Acme", source=mid)
        assert (await gm.export(agent_id="alice"))["edges"][0]["sources"] == [mid]
    finally:
        gm.close()


@needs_ladybug
async def test_graph_html_shows_visible_sources_and_counts_the_rest(
        tmp_path, monkeypatch, memory):
    gm = _gm(tmp_path)
    monkeypatch.setattr(graph_store, "_graph", gm)
    try:
        shared = await memory.store(content="kbots uses LadybugDB for the graph.",
                                    type="semantic", agent_id="alice", scope="global")
        secret = await memory.store(content="SECRET: bob's private note about LadybugDB.",
                                    type="semantic", agent_id="bob", scope="private")
        await gm.link("kbots", "uses", "LadybugDB", scope="global",
                      created_by="alice", source=shared)
        await gm.link("kbots", "uses", "LadybugDB", scope="global",
                      created_by="bob", source=secret)
        await gm.link("kbots", "uses", "LadybugDB", scope="global",
                      created_by="alice", source="forgotten-id")

        ctx = ToolContext(agent_id="alice", memory=memory, project_dir=str(tmp_path))
        edges = await _render(ctx, tmp_path, view="shared")
        edge = edges[0]
        assert [p["id"] for p in edge["provenance"]] == [shared]
        assert edge["provenance"][0]["text"].startswith("kbots uses LadybugDB")
        assert edge["provenance"][0]["created_by"] == "alice"
        # private and forgotten are indistinguishable: a count, no ids, no text
        assert edge["hidden_sources"] == 2
        assert "sources" not in edge
        html = next(tmp_path.glob("memory-graph-*.html")).read_text()
        assert "SECRET" not in html and secret not in html
        assert "forgotten-id" not in html

        # the owner of the private memory sees its text
        bob = ToolContext(agent_id="bob", memory=memory, project_dir=str(tmp_path / "b"))
        edge = (await _render(bob, tmp_path, view="shared"))[0]
        assert {p["id"] for p in edge["provenance"]} == {shared, secret}
        assert edge["hidden_sources"] == 1
    finally:
        gm.close()


@needs_ladybug
async def test_fleet_view_resolves_every_scope(tmp_path, monkeypatch, memory):
    from src.tools import graph as graph_tools
    gm = _gm(tmp_path)
    monkeypatch.setattr(graph_store, "_graph", gm)
    monkeypatch.setattr(graph_tools, "_may_view_all", lambda agent_id: True)
    try:
        secret = await memory.store(content="bob private", type="semantic",
                                    agent_id="bob", scope="private")
        await gm.link("X", "uses", "Y", scope="agent", created_by="bob", source=secret)
        ctx = ToolContext(agent_id="jarvis", memory=memory, project_dir=str(tmp_path))
        edge = (await _render(ctx, tmp_path, view="all"))[0]
        assert [p["id"] for p in edge["provenance"]] == [secret]
        assert edge["hidden_sources"] == 0
    finally:
        gm.close()


@needs_ladybug
async def test_snippets_are_truncated_and_inferred_ids_are_flagged(
        tmp_path, monkeypatch, memory):
    gm = _gm(tmp_path)
    monkeypatch.setattr(graph_store, "_graph", gm)
    try:
        mid = await memory.store(content="x" * 1000, type="semantic",
                                 agent_id="alice", scope="global")
        await gm.link("A", "uses", "B", scope="global", created_by="alice",
                      source=INFERRED_PREFIX + mid)
        ctx = ToolContext(agent_id="alice", memory=memory, project_dir=str(tmp_path))
        prov = (await _render(ctx, tmp_path, view="shared"))[0]["provenance"]
        assert prov[0]["id"] == mid and prov[0]["inferred"] is True
        assert len(prov[0]["text"]) <= 301
    finally:
        gm.close()


@needs_ladybug
async def test_graph_renders_without_a_memory_backend(tmp_path, monkeypatch):
    gm = _gm(tmp_path)
    monkeypatch.setattr(graph_store, "_graph", gm)
    try:
        await gm.link("A", "uses", "B", scope="agent", created_by="alice", source="m1")
        ctx = ToolContext(agent_id="alice", project_dir=str(tmp_path))
        edge = (await _render(ctx, tmp_path))[0]
        assert edge["provenance"] == [] and edge["hidden_sources"] == 1
    finally:
        gm.close()


# --- backfill ---

async def _raw_edge(conn, a, rel, b):
    for name in (a, b):
        await conn.execute("MERGE (e:Entity {name: $n}) ON CREATE SET e.type = 'entity'",
                           {"n": name})
    await conn.execute(
        "MATCH (a:Entity {name: $a}), (b:Entity {name: $b}) "
        "CREATE (a)-[r:Related {rel: $rel, confidence: 0.7, scope: 'global', "
        "created_by: 'old', created_at: '2026-06-01'}]->(b)",
        {"a": a, "b": b, "rel": rel})


async def _sources(conn, a):
    rows = graph_store._rows(await conn.execute(
        "MATCH (a:Entity {name: $a})-[r:Related]->(b:Entity) RETURN r.sources AS s",
        {"a": a}))
    return parse_sources(rows[0]["s"])


@needs_ladybug
async def test_backfill_infers_sources_from_memories_anchoring_both_ends(tmp_path, memory):
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_edge(conn, "Blue Fox", "uses", "Bandpost")
        await _raw_edge(conn, "Lonely", "uses", "Nothing")
        both = await memory.store(content="Blue Fox posts to Bandpost.", type="semantic",
                                  agent_id="t", scope="global")
        one = await memory.store(content="Blue Fox alone.", type="semantic",
                                 agent_id="t", scope="global")
        await memory.anchor_entities(both, ["Blue Fox", "Bandpost"])
        await memory.anchor_entities(one, ["Blue Fox"])

        dry = backfill.Report()
        await backfill.pass_sources(conn, memory, False, dry)
        assert dry.counts["edges given inferred sources"] == 1
        assert await _sources(conn, "Blue Fox") == []

        rep = backfill.Report()
        await backfill.pass_sources(conn, memory, True, rep)
        assert await _sources(conn, "Blue Fox") == [INFERRED_PREFIX + both]
        assert rep.counts["edges with no inferable source"] == 1
        assert await _sources(conn, "Lonely") == []

        again = backfill.Report()
        await backfill.pass_sources(conn, memory, True, again)
        assert again.counts["edges given inferred sources"] == 0
    finally:
        gm.close()


@needs_ladybug
async def test_backfill_leaves_exact_sources_alone(tmp_path, memory):
    gm = _gm(tmp_path)
    try:
        await gm.link("Blue Fox", "uses", "Bandpost", scope="global",
                      created_by="a", source="exact")
        conn = await gm._ensure_open()
        mid = await memory.store(content="Blue Fox posts to Bandpost.", type="semantic",
                                 agent_id="t", scope="global")
        await memory.anchor_entities(mid, ["Blue Fox", "Bandpost"])
        await backfill.pass_sources(conn, memory, True, backfill.Report())
        assert await _sources(conn, "Blue Fox") == ["exact"]
    finally:
        gm.close()
