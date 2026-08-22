"""Entity resolution and bitemporal edges in the graph store.

Two faults, both measured on the live fleet graph:

Resolution was `MERGE (e:Entity {name: $name})` on an exact string, so there
was no resolution at all: `Dr.Zoid`, `Dr. Zoid` and `dr zoid` were three nodes
holding a third of each other's edges.

Nothing ever expired. A re-link did `ON MATCH SET r.confidence` and left both
values in place, so a fact that was true in June and the fact that replaced it
in August were indistinguishable, and a contradiction could not be detected
because there was no "currently valid" to compare against.
"""

import pytest

from src.lib.graph_store import GraphMemory

try:
    import ladybug  # noqa: F401
    _missing = False
except ImportError:
    _missing = True

needs_ladybug = pytest.mark.skipif(_missing, reason="ladybug not installed")


def _gm(tmp_path, name="g.lbdb") -> GraphMemory:
    return GraphMemory({"enabled": True, "path": str(tmp_path / name)})


# --- entity resolution ---

@needs_ladybug
async def test_a_second_spelling_becomes_an_alias_not_a_second_node(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("Dr. Zoid", "works_on", "LinkedIn", scope="global", created_by="a")
        edge = await gm.link("Dr.Zoid", "uses", "Canva", scope="global", created_by="a")
        assert edge["a"] == "Dr. Zoid", "the second spelling created its own node"
        names = {e["name"] for e in await gm.entities(agent_id="a")}
        assert "Dr.Zoid" not in names
    finally:
        gm.close()


@needs_ladybug
async def test_resolution_survives_a_reopen(tmp_path):
    """Two spellings arriving in different extraction passes, and therefore in
    different processes, is the normal case rather than the edge case. If the
    index were built from the current batch instead of from the store, this is
    where duplicates would come back.
    """
    gm = _gm(tmp_path)
    try:
        await gm.link("Neon Husky", "published_on", "TikTok", scope="global", created_by="a")
    finally:
        gm.close()
    gm = _gm(tmp_path)
    try:
        edge = await gm.link("neon-husky", "uses", "Jamendo", scope="global", created_by="a")
        assert edge["a"] == "Neon Husky"
    finally:
        gm.close()


@needs_ladybug
async def test_the_alternative_spelling_is_kept_so_the_merge_is_auditable(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("Contribution Run", "part_of", "kbots", scope="global", created_by="a")
        await gm.link("contribution-run", "uses", "Phaser", scope="global", created_by="a")
        conn = await gm._ensure_open()
        from src.lib.graph_store import _rows
        rows = _rows(await conn.execute(
            "MATCH (e:Entity {name: 'Contribution Run'}) RETURN e.aliases AS aliases"))
        assert "contribution-run" in (rows[0]["aliases"] or "")
    finally:
        gm.close()


@needs_ladybug
async def test_relations_are_normalised_on_write(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("Kristian", "employed_by", "Acme", scope="global", created_by="a")
        edges = await gm.related("Kristian", agent_id="a")
        assert edges[0]["rel"] == "works_at"
    finally:
        gm.close()


@needs_ladybug
async def test_a_query_by_synonym_finds_the_canonical_edge(tmp_path):
    """Normalised on the way in and on the way out. Only one of the two makes
    the store consistent; both make it usable.
    """
    from src.core.base import ToolContext
    from src.lib import graph_store
    from src.tools.graph import memory_graph_query
    gm = _gm(tmp_path)
    graph_store._graph = gm
    try:
        await gm.link("kbots", "hosted_on", "Hostinger", scope="global", created_by="a")
        out = await memory_graph_query(ToolContext(agent_id="a"), rel="deployed_on")
        assert "Hostinger" in out
    finally:
        graph_store._graph = None
        gm.close()


# --- bitemporality ---

@needs_ladybug
async def test_a_new_value_of_a_single_valued_fact_closes_the_old_one(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("Kristian", "works_at", "OldCo", scope="global", created_by="a")
        result = await gm.link("Kristian", "works_at", "NewCo", scope="global", created_by="a")
        assert result.get("superseded") == 1
        current = await gm.related("Kristian", agent_id="a")
        assert [e["dst"] for e in current] == ["NewCo"]
    finally:
        gm.close()


@needs_ladybug
async def test_the_superseded_fact_is_still_answerable_as_history(tmp_path):
    """Supersession is not deletion. "Where did they work before" has an
    answer, and the timestamps exist precisely so that it does.
    """
    gm = _gm(tmp_path)
    try:
        await gm.link("Kristian", "works_at", "OldCo", scope="global", created_by="a")
        await gm.link("Kristian", "works_at", "NewCo", scope="global", created_by="a")
        history = await gm.history("Kristian", agent_id="a")
        by_dst = {h["dst"]: h for h in history}
        assert by_dst["OldCo"]["current"] is False
        assert by_dst["OldCo"]["valid_to"] is not None
        assert by_dst["NewCo"]["current"] is True
        assert by_dst["NewCo"]["valid_to"] is None
    finally:
        gm.close()


@needs_ladybug
async def test_a_multi_valued_relation_accumulates_instead_of_superseding(tmp_path):
    """A project genuinely uses many tools. Expiring the previous one on every
    new fact would be the most destructive possible reading of a correct edge.
    """
    gm = _gm(tmp_path)
    try:
        await gm.link("kbots", "uses", "SQLite", scope="global", created_by="a")
        await gm.link("kbots", "uses", "LadybugDB", scope="global", created_by="a")
        current = {e["dst"] for e in await gm.related("kbots", agent_id="a")}
        assert current == {"SQLite", "LadybugDB"}
    finally:
        gm.close()


@needs_ladybug
async def test_re_asserting_the_current_fact_does_not_revive_an_expired_edge(tmp_path):
    """The subtle one. MERGE cannot express "the edge that is still open", so
    a naive re-link matches the expired edge and quietly resurrects it: the
    entity then has two current employers again, one of which was retired.
    """
    gm = _gm(tmp_path)
    try:
        await gm.link("Kristian", "works_at", "OldCo", scope="global", created_by="a")
        await gm.link("Kristian", "works_at", "NewCo", scope="global", created_by="a")
        await gm.link("Kristian", "works_at", "OldCo", scope="global", created_by="a")
        current = [e["dst"] for e in await gm.related("Kristian", agent_id="a")]
        assert current == ["OldCo"], "re-asserting should supersede, not duplicate"
        assert len(await gm.history("Kristian", agent_id="a")) >= 2
    finally:
        gm.close()


@needs_ladybug
async def test_expired_edges_are_absent_from_every_current_read(tmp_path):
    """One filter, applied everywhere. A read that forgets it shows a retired
    fact beside the fact that replaced it, which reads as a contradiction in
    the data rather than as a bug in the query.
    """
    gm = _gm(tmp_path)
    try:
        await gm.link("Kristian", "located_in", "Malmo", scope="global", created_by="a")
        await gm.link("Kristian", "located_in", "Stockholm", scope="global", created_by="a")

        assert [e["dst"] for e in await gm.related("Kristian", agent_id="a")] == ["Stockholm"]
        assert [e["dst"] for e in await gm.find(entity="Kristian", agent_id="a")] == ["Stockholm"]
        exported = await gm.export(agent_id="a")
        assert [e["dst"] for e in exported["edges"]] == ["Stockholm"]
        assert "Malmo" not in {n["name"] for n in exported["nodes"]}
    finally:
        gm.close()


@needs_ladybug
async def test_the_loopback_client_exposes_history_too(tmp_path):
    """Tool subprocesses reach the graph through GraphClient, not GraphMemory.

    A method added to one and not the other, or added to the client but not to
    the internal API's allow-list, gives a tool that works in the engine and
    returns "unknown graph method" from every agent. Both sides are pinned
    here rather than discovered in production.
    """
    from src.core.internal_api import _GRAPH_METHODS
    from src.lib.graph_store import GraphClient
    for method in ("link", "related", "unlink", "entities", "export", "find", "history"):
        assert hasattr(GraphClient, method), f"GraphClient is missing {method}"
        assert method in _GRAPH_METHODS, f"the /graph route rejects {method}"


@needs_ladybug
async def test_history_is_the_one_read_that_shows_everything(tmp_path):
    gm = _gm(tmp_path)
    try:
        await gm.link("Kristian", "located_in", "Malmo", scope="global", created_by="a")
        await gm.link("Kristian", "located_in", "Stockholm", scope="global", created_by="a")
        assert {h["dst"] for h in await gm.history("Kristian", agent_id="a")} == \
            {"Malmo", "Stockholm"}
    finally:
        gm.close()


@needs_ladybug
async def test_history_is_scope_filtered_like_every_other_read(tmp_path):
    """A history view that ignored scope would be a way to read another
    agent's private edges, and a more complete one than any current read.
    """
    gm = _gm(tmp_path)
    try:
        await gm.link("Secret", "works_at", "Place", scope="agent", created_by="alice")
        assert await gm.history("Secret", agent_id="bob") == []
        assert await gm.history("Secret", agent_id="alice") != []
    finally:
        gm.close()


@needs_ladybug
async def test_unlink_removes_expired_versions_too(tmp_path):
    """Supersession is for a fact that changed; unlink is for a fact that
    should never have been recorded. Leaving the expired copy behind would make
    unlink a worse version of the changelog retention bug: deleted by every
    query, still on disk in full.
    """
    gm = _gm(tmp_path)
    try:
        await gm.link("Kristian", "works_at", "Wrong", scope="global", created_by="a")
        await gm.link("Kristian", "works_at", "AlsoWrong", scope="global", created_by="a")
        await gm.unlink("Kristian", "works_at", "Wrong", agent_id="a")
        assert [h["dst"] for h in await gm.history("Kristian", agent_id="a")] == ["AlsoWrong"]
    finally:
        gm.close()


@needs_ladybug
async def test_unlink_resolves_spellings_and_synonyms(tmp_path):
    """Otherwise an agent cannot remove an edge it just created: it wrote
    'Dr.Zoid uses Canva', the store holds 'Dr. Zoid', and unlink by the name it
    used silently removes nothing.
    """
    gm = _gm(tmp_path)
    try:
        await gm.link("Dr. Zoid", "utilizes", "Canva", scope="global", created_by="a")
        assert await gm.unlink("Dr.Zoid", "uses", "Canva", agent_id="a") == 1
        assert await gm.related("Dr. Zoid", agent_id="a") == []
    finally:
        gm.close()
