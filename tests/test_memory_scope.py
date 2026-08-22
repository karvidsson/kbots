"""Who can read whose memory.

Measured on the live store on 2026-08-22: 237 memories, of which 235 were
`agent:<id>` and 0 were `global`. Every agent was searching a corpus of its own
handful of notes while six weeks of the fleet's lessons sat one row away and
unreadable. Nothing chose that. `memory_store` defaults to scope 'agent' and no
caller ever passed anything else, so a default became a policy.

Two of the four scopes also did not work. `group:<name>` matched `group:%` for
every agent, so it was a global scope with a reassuring name, and there was no
way at all to keep a note to yourself once agent scope became fleet-readable.
"""

import asyncio

import pytest

from src.memory.recall import format_block
from src.memory.sqlite import SQLiteMemory


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path, fake_embeddings):
    def make(**cfg):
        return SQLiteMemory(config={"path": str(tmp_path / "m.db"), **cfg})
    return make


def _seed(mem, agent, scope, content, target=None):
    return run(mem.store(content=content, type="semantic", agent_id=agent,
                         scope=scope, scope_target=target or agent))


def _visible(mem, agent, query="lesson"):
    return {m["content"] for m in run(mem.search(query=query, agent_id=agent, limit=50))}


def test_one_agents_lesson_is_readable_by_another(store):
    """The whole point. This is the 235 memories that nobody but their author
    could see, and the reason every agent kept relearning the same things.
    """
    mem = store()
    _seed(mem, "husky", "agent:husky", "lesson about ffmpeg peak levels")
    assert "lesson about ffmpeg peak levels" in _visible(mem, "tally")


def test_a_private_memory_is_never_readable_by_another(store):
    """Sharing by default is only defensible if there is somewhere to put the
    things that should not be shared.
    """
    mem = store()
    _seed(mem, "husky", "private:husky", "lesson kept to myself")
    assert _visible(mem, "tally", "lesson kept myself") == set()
    assert "lesson kept to myself" in _visible(mem, "husky", "lesson kept myself")


def test_fleet_read_can_be_turned_off(store):
    """A deployment whose agents work for different people needs the old
    behaviour, and a change this broad needs to be reversible by config rather
    than by a revert.
    """
    mem = store(fleet_read=False)
    _seed(mem, "husky", "agent:husky", "lesson about ffmpeg peak levels")
    assert _visible(mem, "tally") == set()
    assert _visible(mem, "husky") != set()


def test_global_is_readable_with_fleet_read_off(store):
    mem = store(fleet_read=False)
    _seed(mem, "husky", "global", "the deploy gate is ruff plus pytest")
    assert "the deploy gate is ruff plus pytest" in _visible(mem, "tally", "deploy gate")


def test_private_beats_fleet_read_even_for_the_scope_target(store):
    """scope_target is a second path to a memory, and it must not become a way
    around `private`. Same defect shape as `group:%`: a check that looks like a
    restriction and grants everything.
    """
    mem = store()
    run(mem.store(content="private note with a target", type="semantic",
                  agent_id="husky", scope="private:husky", scope_target="tally"))
    assert _visible(mem, "tally", "private note target") == set()


def test_a_group_memory_is_readable_only_by_its_members(store):
    mem = store(groups={"money": ["tally", "rainmaker"]})
    _seed(mem, "tally", "group:money", "the ISK report is a two-tool pipeline")
    assert "the ISK report is a two-tool pipeline" in _visible(mem, "rainmaker", "ISK report")
    assert _visible(mem, "husky", "ISK report") == set()


def test_a_group_with_no_configured_members_is_readable_by_nobody(store):
    """Regression: the filter matched `group:%`, so any group scope was visible
    to every agent. Closed rather than left open, because a scope that grants
    more than it names is worse than one that grants nothing.
    """
    mem = store()
    _seed(mem, "tally", "group:money", "the ISK report is a two-tool pipeline")
    assert _visible(mem, "husky", "ISK report") == set()
    # The author still reads it: they wrote it and it is filed under their name.
    assert "the ISK report is a two-tool pipeline" in _visible(mem, "tally", "ISK report")


def test_the_agent_scope_still_belongs_to_its_author(store):
    """Fleet-readable is not ownerless. Author attribution is what makes a
    borrowed lesson usable, and forget/scope checks depend on it.
    """
    mem = store()
    mid = _seed(mem, "husky", "agent:husky", "lesson about ffmpeg peak levels")
    row = mem.db.execute("SELECT created_by, scope FROM memories WHERE id = ?",
                         (mid,)).fetchone()
    assert (row["created_by"], row["scope"]) == ("husky", "agent:husky")


def test_semantic_search_applies_the_same_rules(store):
    """Three engines each apply scope themselves. One that forgets is a leak
    that keyword tests would not catch.
    """
    mem = store()
    _seed(mem, "husky", "private:husky", "private note about the master wav")
    results = run(mem.semantic_search(query="private note master wav", agent_id="tally"))
    assert [r for r in results if "master wav" in r["content"]] == []


def test_a_recalled_memory_says_who_learned_it(store):
    """Unattributed, another agent's lesson about its own distributor reads as
    something this agent knows. Fleet-wide read without attribution would make
    every agent confidently wrong about six other domains.
    """
    block = format_block(
        [{"content": "CD Baby locks release metadata", "category": "lesson",
          "created_by": "husky"}], agent_id="tally")
    assert "learned by husky" in block


def test_your_own_memory_is_not_labelled_with_your_own_name(store):
    block = format_block(
        [{"content": "my own note", "category": "lesson", "created_by": "tally"}],
        agent_id="tally")
    assert "learned by" not in block


def test_private_is_accepted_by_the_store_tool(store, monkeypatch):
    from src.core.base import ToolContext
    from src.tools.memory import memory_store

    mem = store()
    ctx = ToolContext(agent_id="husky")
    ctx.memory = mem
    out = run(memory_store(ctx, content="a note to myself", scope="private"))
    assert "Stored memory" in out
    row = mem.db.execute("SELECT scope FROM memories").fetchone()
    assert row["scope"] == "private:husky"


def test_an_unknown_scope_is_rejected_rather_than_coerced(store):
    from src.core.base import ToolContext
    from src.tools.memory import memory_store

    mem = store()
    ctx = ToolContext(agent_id="husky")
    ctx.memory = mem
    out = run(memory_store(ctx, content="x", scope="secret"))
    assert "Invalid scope" in out
    assert mem.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
