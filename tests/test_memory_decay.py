"""Memory decay: it runs, it spares lessons, and it does not delete.

Measured on the live store on 2026-08-22: 237 memories, 236 still at the birth
confidence of 0.70, the oldest created seven weeks earlier, zero archived, and
no decay row in the changelog. It had never run once, on a deployment whose
config said `decay_enabled: true`.

Three halves of one mechanism, none wired to the others. `SQLiteMemory.decay()`
was complete and had no caller. `decay_enabled` was read by the config schema
and the settings manager and by nothing in the engine. `memory-decay.sh`
implemented the lifecycle a second time and was scheduled by a systemd timer
that no macOS install has.

The tests are grouped the same way: does it run, does it spare what must not
fade, and does it refrain from deleting.
"""

import asyncio

import pytest

from src.core.memory_decay import MemoryDecay


def run(coro):
    return asyncio.run(coro)


def _age(mem, memory_id, days=30, confidence=None):
    """Backdate a memory's last access, and optionally set its confidence."""
    mem.db.execute(
        "UPDATE memories SET last_accessed = datetime('now', ?) WHERE id = ?",
        (f"-{days} days", memory_id))
    if confidence is not None:
        mem.db.execute("UPDATE memories SET confidence = ? WHERE id = ?",
                       (confidence, memory_id))
    mem.db.commit()


def _row(mem, memory_id):
    return mem.db.execute(
        "SELECT confidence, scope, pinned FROM memories WHERE id = ?",
        (memory_id,)).fetchone()


# --- it runs at all ---

def test_the_engine_starts_a_decay_task_when_the_config_says_so(memory):
    """The gap that made every other property here moot: nothing read the flag.

    `decay_enabled` was in config_schema.py and settings.py and nowhere else,
    so a deployment could set it, see it in the settings manager, and get
    nothing.
    """
    assert MemoryDecay(memory, {"decay_enabled": True}).enabled is True
    assert MemoryDecay(memory, {"decay_enabled": False}).enabled is False
    assert MemoryDecay(memory, {}).enabled is False, "decay must be opt-in"


def test_a_tick_actually_decays(memory):
    async def go():
        mid = await memory.store(content="an unremarkable note", type="semantic",
                                 agent_id="t", category="general")
        _age(memory, mid, days=2)
        before = _row(memory, mid)["confidence"]
        result = await MemoryDecay(memory, {"decay_enabled": True}).tick()
        return before, _row(memory, mid)["confidence"], result

    before, after, result = run(go())
    assert after < before
    assert result["decayed"] == 1


def test_the_decay_is_recorded_where_it_can_be_audited(memory):
    """Nothing had a decay row in the changelog, which is how "it has never run"
    was provable rather than suspected. That evidence has to keep existing.
    """
    async def go():
        mid = await memory.store(content="an unremarkable note", type="semantic",
                                 agent_id="t", category="general")
        _age(memory, mid, days=2)
        await MemoryDecay(memory, {"decay_enabled": True}).tick()

    run(go())
    rows = memory.db.execute(
        "SELECT COUNT(*) FROM changelog WHERE action = 'decay'").fetchone()
    assert rows[0] == 1


def test_a_recalled_memory_does_not_decay(memory):
    """The whole point of decay is that use keeps a memory alive. Search sets
    last_accessed, so anything the fleet actually reaches for never fades.
    """
    async def go():
        mid = await memory.store(content="a note about the deploy gate",
                                 type="semantic", agent_id="t", category="general",
                                 scope="global")
        _age(memory, mid, days=30)
        await memory.search(query="deploy gate", agent_id="t")   # resets the clock
        before = _row(memory, mid)["confidence"]
        await MemoryDecay(memory, {"decay_enabled": True}).tick()
        return before, _row(memory, mid)["confidence"]

    before, after = run(go())
    assert after == before


# --- what must never fade ---

def test_a_lesson_never_decays(memory):
    """A lesson exists because something went wrong once and somebody paid for
    finding out. 156 of the live store's 237 memories are lessons. A lesson
    unrecalled for sixty days is not stale, it is a mistake that has not
    recurred yet.
    """
    async def go():
        mid = await memory.store(content="never measure peak level with -ac 1",
                                 type="semantic", agent_id="t", category="lesson")
        _age(memory, mid, days=90)
        before = _row(memory, mid)["confidence"]
        await MemoryDecay(memory, {"decay_enabled": True}).tick()
        return before, _row(memory, mid)["confidence"]

    before, after = run(go())
    assert after == before


def test_a_lesson_is_never_archived_even_at_zero_confidence(memory):
    """Immunity has to hold at the archive step too, not only the decay step.

    A lesson that reached a low confidence some other way (a run before this
    fix, a manual edit, a future negative-feedback path) would otherwise be
    archived on the next tick despite never being decayed by one.
    """
    async def go():
        mid = await memory.store(content="a hard-won lesson", type="semantic",
                                 agent_id="t", category="lesson")
        _age(memory, mid, days=90, confidence=0.0)
        await MemoryDecay(memory, {"decay_enabled": True}).tick()
        return _row(memory, mid)

    assert not run(go())["scope"].startswith("archived")


def test_a_pinned_memory_never_decays(memory):
    async def go():
        mid = await memory.store(content="a pinned fact", type="semantic",
                                 agent_id="t", category="general", pinned=1)
        _age(memory, mid, days=90)
        before = _row(memory, mid)["confidence"]
        await MemoryDecay(memory, {"decay_enabled": True}).tick()
        return before, _row(memory, mid)["confidence"]

    before, after = run(go())
    assert after == before


# --- archiving, and the refusal to delete ---

def test_a_faded_memory_is_archived_and_disappears_from_reads(memory):
    async def go():
        mid = await memory.store(content="a forgotten note about kangaroos",
                                 type="semantic", agent_id="t", category="general",
                                 scope="global")
        _age(memory, mid, days=90, confidence=0.01)
        await MemoryDecay(memory, {"decay_enabled": True}).tick()
        return mid, await memory.search(query="kangaroos", agent_id="t")

    mid, hits = run(go())
    assert _row(memory, mid)["scope"].startswith("archived:")
    assert hits == [], "an archived memory must not surface in search"


def test_archiving_keeps_the_original_scope_so_it_can_be_undone(memory):
    """Archiving is reversible by editing one column. That is the property
    that makes it a safe default where deleting is not.
    """
    async def go():
        mid = await memory.store(content="a forgotten note", type="semantic",
                                 agent_id="t", category="general", scope="global")
        _age(memory, mid, days=90, confidence=0.01)
        await MemoryDecay(memory, {"decay_enabled": True}).tick()
        return mid

    mid = run(go())
    assert _row(memory, mid)["scope"] == "archived:global"


def test_nothing_is_deleted_by_default(memory):
    """The change that made decay safe to turn on. Purging used to be
    unconditional, so enabling decay meant "delete in 150 days" on a store
    where nothing had ever been recalled.
    """
    async def go():
        mid = await memory.store(content="an ancient archived note", type="semantic",
                                 agent_id="t", category="general")
        memory.db.execute(
            "UPDATE memories SET scope = 'archived:global', "
            "updated_at = datetime('now', '-400 days') WHERE id = ?", (mid,))
        memory.db.commit()
        result = await MemoryDecay(memory, {"decay_enabled": True}).tick()
        return mid, result

    mid, result = run(go())
    assert result["purged"] == 0
    assert result["purge_enabled"] is False
    assert _row(memory, mid) is not None, "decay deleted a memory with purge off"


def test_purging_is_available_when_a_deployment_asks_for_it(memory):
    """Off by default is not the same as removed. A deployment that wants a
    bounded store can still have one.
    """
    async def go():
        mid = await memory.store(content="an ancient archived note", type="semantic",
                                 agent_id="t", category="general")
        memory.db.execute(
            "UPDATE memories SET scope = 'archived:global', "
            "updated_at = datetime('now', '-400 days') WHERE id = ?", (mid,))
        memory.db.commit()
        cfg = {"decay_enabled": True, "decay": {"purge_archived": True}}
        return mid, await MemoryDecay(memory, cfg).tick()

    mid, result = run(go())
    assert result["purged"] == 1
    assert _row(memory, mid) is None


def test_purging_spares_a_recently_archived_memory(memory):
    async def go():
        mid = await memory.store(content="a recently archived note", type="semantic",
                                 agent_id="t", category="general")
        memory.db.execute(
            "UPDATE memories SET scope = 'archived:global', "
            "updated_at = datetime('now', '-10 days') WHERE id = ?", (mid,))
        memory.db.commit()
        cfg = {"decay_enabled": True, "decay": {"purge_archived": True}}
        return mid, await MemoryDecay(memory, cfg).tick()

    mid, result = run(go())
    assert result["purged"] == 0
    assert _row(memory, mid) is not None


# --- configuration ---

def test_settings_come_from_config_rather_than_from_the_defaults_in_code(memory):
    cfg = {"decay_enabled": True,
           "decay": {"interval_hours": 6, "rate": 0.5, "archive_threshold": 0.4,
                     "purge_archived": True, "purge_after_days": 7}}
    d = MemoryDecay(memory, cfg)
    assert (d.interval, d.rate, d.threshold) == (6 * 3600, 0.5, 0.4)
    assert d.purge is True and d.purge_after_days == 7


@pytest.mark.parametrize("key", ["decay_enabled", "decay"])
def test_the_config_keys_are_documented(key):
    """A key the engine reads and the example config never mentions is a
    setting nobody can find. `decay_enabled` was documented and unread; the
    inverse is just as bad.
    """
    from pathlib import Path

    from src.core.base import PROJECT_ROOT
    example = Path(PROJECT_ROOT / "config" / "config.yaml.example").read_text()
    assert f"{key}:" in example


def test_the_retired_shell_script_does_not_touch_the_database():
    """It implemented the same lifecycle a second time, against a path it
    resolved itself, which stopped being the live store when data_dir moved.
    Two implementations of one policy is how they drift; this one is a message
    now, not a mechanism.
    """
    from pathlib import Path

    from src.core.base import PROJECT_ROOT
    script = Path(PROJECT_ROOT / "scripts" / "memory-decay.sh").read_text()
    assert "UPDATE memories" not in script
    assert "DELETE FROM memories" not in script
    assert "src/core/memory_decay.py" in script
