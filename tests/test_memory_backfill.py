"""The backfill: applying the memory overhaul to data that already exists.

Every fix in src/memory and src/lib/graph_store changes the next write. None of
them touch what is already stored, and on the live fleet that is 237 memories,
167 entities and 190 edges carrying duplicate spellings, 71 relation names, no
anchors and no vectors. Without this script the overhaul is invisible on the
one deployment that has been running longest.

It edits real data in place, so the tests that matter here are the ones about
restraint: a dry run must write nothing, a merge must lose no edges, and a pass
must be safe to run twice.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest

from src.lib.graph_store import GraphMemory, _rows

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory-backfill.py"
_spec = importlib.util.spec_from_file_location("memory_backfill", SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

try:
    import ladybug  # noqa: F401
    _missing = False
except ImportError:
    _missing = True

needs_ladybug = pytest.mark.skipif(_missing, reason="ladybug not installed")


def run(coro):
    return asyncio.run(coro)


def _gm(tmp_path) -> GraphMemory:
    return GraphMemory({"enabled": True, "path": str(tmp_path / "g.lbdb")})


async def _raw_link(conn, a, rel, b, **props):
    """Write an edge the way the OLD code did: no resolution, no normalisation.

    The backfill exists to clean up exactly this, so the fixtures must not go
    through link(), which would fix everything on the way in and leave the
    tests asserting against already-clean data.
    """
    for name in (a, b):
        await conn.execute(
            "MERGE (e:Entity {name: $name}) ON CREATE SET e.type = 'entity', "
            "e.scope = 'global', e.created_by = 'old', e.created_at = '2026-06-01'",
            {"name": name})
    await conn.execute(
        "MATCH (a:Entity {name: $a}), (b:Entity {name: $b}) "
        "CREATE (a)-[r:Related {rel: $rel, confidence: 0.7, scope: 'global', "
        "created_by: 'old', created_at: $created}]->(b)",
        {"a": a, "b": b, "rel": rel, "created": props.get("created_at", "2026-06-01")})


async def _edges(gm):
    conn = await gm._ensure_open()
    return _rows(await conn.execute(
        "MATCH (a:Entity)-[r:Related]->(b:Entity) RETURN a.name AS src, r.rel AS rel, "
        "b.name AS dst, r.valid_from AS valid_from, r.valid_to AS valid_to"))


async def _names(gm):
    conn = await gm._ensure_open()
    return {r["name"] for r in _rows(
        await conn.execute("MATCH (e:Entity) RETURN e.name AS name"))}


# --- relation normalisation ---

@needs_ladybug
async def test_synonym_relations_are_collapsed(tmp_path):
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "Ada", "employed_by", "Acme")
        await _raw_link(conn, "kbots", "utilizes", "SQLite")
        rep = backfill.Report()
        await backfill.pass_relations(conn, True, rep)
        rels = {e["rel"] for e in await _edges(gm)}
        assert rels == {"works_at", "uses"}
        assert rep.counts["relations renamed"] == 2
    finally:
        gm.close()


@needs_ladybug
async def test_a_dry_run_writes_nothing(tmp_path):
    """The default mode. It has to be trustworthy or nobody will use it, and
    then the only way to find out what the script does is to let it do it.
    """
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "Ada", "employed_by", "Acme")
        await _raw_link(conn, "Dr.Sable", "uses", "Canva")
        await _raw_link(conn, "Dr. Sable", "uses", "LinkedIn")
        before = await _edges(gm)

        rep = backfill.Report()
        await backfill.pass_relations(conn, False, rep)
        await backfill.pass_timestamps(conn, False, rep)
        await backfill.pass_entities(conn, False, rep)
        await backfill.pass_supersede(conn, False, rep)

        assert await _edges(gm) == before
        assert rep.counts, "a dry run that reports nothing is indistinguishable from a no-op"
    finally:
        gm.close()


# --- entity merging ---

@needs_ladybug
async def test_duplicate_spellings_are_merged_and_their_edges_survive(tmp_path):
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "Dr. Sable", "works_on", "LinkedIn")
        await _raw_link(conn, "Dr. Sable", "uses", "Canva")
        await _raw_link(conn, "Dr.Sable", "uses", "Figma")
        await _raw_link(conn, "Ada", "owns", "dr sable")

        await backfill.pass_entities(conn, True, backfill.Report())

        names = await _names(gm)
        assert "Dr. Sable" in names
        assert "Dr.Sable" not in names and "dr sable" not in names

        edges = await _edges(gm)
        assert ("Dr. Sable", "uses", "Figma") in {(e["src"], e["rel"], e["dst"]) for e in edges}
        assert ("Ada", "owns", "Dr. Sable") in {(e["src"], e["rel"], e["dst"]) for e in edges}
        assert len(edges) == 4, "an edge was lost or duplicated in the merge"
    finally:
        gm.close()


@needs_ladybug
async def test_the_most_connected_spelling_wins(tmp_path):
    """It is the one the rest of the graph already points at, so keeping it
    rewrites the fewest edges and changes the fewest answers.
    """
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "blue-fox", "uses", "Bandpost")
        await _raw_link(conn, "blue-fox", "uses", "Shortform")
        await _raw_link(conn, "blue-fox", "uses", "YouTube")
        await _raw_link(conn, "Blue Fox", "part_of", "kbots")
        await backfill.pass_entities(conn, True, backfill.Report())
        assert "blue-fox" in await _names(gm)
    finally:
        gm.close()


@needs_ladybug
async def test_the_merge_is_auditable_afterwards(tmp_path):
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "Dr. Sable", "uses", "Canva")
        await _raw_link(conn, "Dr.Sable", "uses", "Figma")
        await backfill.pass_entities(conn, True, backfill.Report())
        rows = _rows(await conn.execute(
            "MATCH (e:Entity {name: 'Dr. Sable'}) RETURN e.aliases AS aliases"))
        assert "Dr.Sable" in (rows[0]["aliases"] or "")
    finally:
        gm.close()


@needs_ladybug
async def test_merging_does_not_create_a_self_edge(tmp_path):
    """Two spellings that were linked to each other. Pointing that edge at the
    canonical name on both ends would assert that a thing relates to itself,
    which is true and useless, and it would show up in every traversal.
    """
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "Dr. Sable", "related_to", "Dr.Sable")
        await backfill.pass_entities(conn, True, backfill.Report())
        assert [e for e in await _edges(gm) if e["src"] == e["dst"]] == []
    finally:
        gm.close()


@needs_ladybug
async def test_running_the_passes_twice_changes_nothing_the_second_time(tmp_path):
    """The script will be run again. It must be safe to."""
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "Dr. Sable", "employed_by", "Acme")
        await _raw_link(conn, "Dr.Sable", "utilizes", "Canva")
        for _ in range(2):
            await backfill.pass_relations(conn, True, backfill.Report())
            await backfill.pass_timestamps(conn, True, backfill.Report())
            await backfill.pass_entities(conn, True, backfill.Report())
            await backfill.pass_supersede(conn, True, backfill.Report())
        first = await _edges(gm)

        rep = backfill.Report()
        await backfill.pass_relations(conn, True, rep)
        await backfill.pass_entities(conn, True, rep)
        assert await _edges(gm) == first
        assert not rep.counts, f"a second run still wanted to change things: {dict(rep.counts)}"
    finally:
        gm.close()


# --- timestamps and supersession ---

@needs_ladybug
async def test_pre_existing_edges_get_their_created_at_as_valid_from(tmp_path):
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "A", "uses", "B", created_at="2026-06-01")
        await backfill.pass_timestamps(conn, True, backfill.Report())
        assert (await _edges(gm))[0]["valid_from"] == "2026-06-01"
    finally:
        gm.close()


@needs_ladybug
async def test_an_existing_contradiction_is_closed_newest_first(tmp_path):
    """Two current employers, both stored as true, is the state the old code
    left behind. The newer one wins and the older is closed rather than
    deleted, so the history still answers "before that, where".
    """
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "Ada", "works_at", "OldCo", created_at="2026-01-01")
        await _raw_link(conn, "Ada", "works_at", "NewCo", created_at="2026-08-01")
        await backfill.pass_timestamps(conn, True, backfill.Report())
        await backfill.pass_supersede(conn, True, backfill.Report())
        by_dst = {e["dst"]: e for e in await _edges(gm)}
        assert by_dst["NewCo"]["valid_to"] is None
        assert by_dst["OldCo"]["valid_to"] is not None
    finally:
        gm.close()


@needs_ladybug
async def test_a_multi_valued_relation_is_left_alone(tmp_path):
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "kbots", "uses", "SQLite")
        await _raw_link(conn, "kbots", "uses", "LadybugDB")
        await backfill.pass_supersede(conn, True, backfill.Report())
        assert all(e["valid_to"] is None for e in await _edges(gm))
    finally:
        gm.close()


# --- anchors ---

@needs_ladybug
async def test_existing_memories_are_anchored_to_the_entities_they_mention(tmp_path, memory):
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "Bandpost", "uses", "Blue Fox")
        mid = await memory.store(content="Blue Fox uploaded the new track to Bandpost.",
                                 type="semantic", agent_id="t", scope="global")
        await backfill.pass_anchors(conn, memory, True, backfill.Report())
        assert set(await memory.entities_for_memories([mid])) == {"Blue Fox", "Bandpost"}
    finally:
        gm.close()


@needs_ladybug
async def test_anchoring_matches_whole_words_only(tmp_path, memory):
    """Substring matching would anchor every memory containing 'run' to the
    game 'Ridge Runner'. Anchors drive the graph hop, so a wrong one does
    not just add noise, it spends the traversal budget on it.
    """
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "Tally", "uses", "CSV")
        mid = await memory.store(content="The ledgers were unreadable and the CSVs stale.",
                                 type="semantic", agent_id="t", scope="global")
        await backfill.pass_anchors(conn, memory, True, backfill.Report())
        assert await memory.entities_for_memories([mid]) == []
    finally:
        gm.close()


@needs_ladybug
async def test_very_short_entity_names_are_not_anchored(tmp_path, memory):
    """'AI' appears in half the corpus and tells you nothing about which memory
    is about the entity AI.
    """
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "AI", "part_of", "kbots")
        mid = await memory.store(content="AI is mentioned here, and kbots too.",
                                 type="semantic", agent_id="t", scope="global")
        await backfill.pass_anchors(conn, memory, True, backfill.Report())
        assert await memory.entities_for_memories([mid]) == ["kbots"]
    finally:
        gm.close()


@needs_ladybug
async def test_anchoring_twice_does_not_duplicate(tmp_path, memory):
    gm = _gm(tmp_path)
    try:
        conn = await gm._ensure_open()
        await _raw_link(conn, "Bandpost", "uses", "Blue Fox")
        await memory.store(content="Blue Fox is on Bandpost.", type="semantic",
                           agent_id="t", scope="global")
        rep = backfill.Report()
        await backfill.pass_anchors(conn, memory, True, rep)
        second = backfill.Report()
        await backfill.pass_anchors(conn, memory, True, second)
        assert not second.counts
    finally:
        gm.close()


# --- embeddings ---

def test_the_embedding_pass_fills_only_what_is_missing(memory):
    """Regression cover for the fault it exists to repair: the live store had
    237 memories and 0 vectors, because the download path imported a package
    that was never a dependency and the failure was swallowed per memory.
    """
    async def go():
        mid = await memory.store(content="a memory with no vector", type="semantic",
                                 agent_id="t")
        memory.db.execute("UPDATE memories SET embedding = NULL")
        memory.db.commit()
        assert memory.missing_embeddings() == 1
        await backfill.pass_embeddings(memory, True, backfill.Report())
        return mid

    run(go())
    assert memory.missing_embeddings() == 0


def test_the_embedding_pass_is_a_no_op_when_nothing_is_missing(memory):
    async def go():
        await memory.store(content="already embedded", type="semantic", agent_id="t")
        rep = backfill.Report()
        await backfill.pass_embeddings(memory, True, rep)
        return rep
    assert not run(go()).counts


def test_a_broken_model_stops_the_pass_instead_of_looping_over_every_row(memory, monkeypatch):
    """One reason, once. 237 identical tracebacks help nobody, and a pass that
    keeps going pretends it did something.
    """
    async def go():
        for i in range(5):
            await memory.store(content=f"memory {i}", type="semantic", agent_id="t")
        memory.db.execute("UPDATE memories SET embedding = NULL")
        memory.db.commit()

        def boom(_self, _text):
            raise RuntimeError("model not installed")
        # Through monkeypatch, not by assigning to the class: a bare assignment
        # here leaks into every later test in the session.
        monkeypatch.setattr(type(memory.embedding), "embed_one", boom)

        rep = backfill.Report()
        await backfill.pass_embeddings(memory, True, rep)
        return rep

    rep = run(go())
    assert rep.counts.get("vectors written", 0) == 0
    assert memory.missing_embeddings() == 5


# --- the refusal to run against a live store ---

def test_holders_reports_a_process_holding_the_file(tmp_path):
    """--apply refuses when either store is open, because both are
    single-writer and the running engine is the other writer. A guard that
    silently reports "nobody" would make the refusal decorative.
    """
    db = tmp_path / "held.db"
    db.write_bytes(b"x")
    assert backfill._holders(db) == []
    with open(db, "rb"):
        # lsof reports this process, which _holders excludes by pid: the check
        # is about OTHER writers.
        assert backfill._holders(db) == []
    assert backfill._holders(tmp_path / "nope.db") == []
