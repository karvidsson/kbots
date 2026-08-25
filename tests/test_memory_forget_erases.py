"""Forgetting a memory must not leave the memory behind.

Regression (2026-08-20): `forget` deleted the row and then wrote the content
straight back into changelog.old_value, while `store` had already written it
into changelog.new_value. A memory deleted on request therefore read clean by
SELECT and was still present, in full, twice. Deleting a row also only unlinks
it — the text stays legible in the freed page, and in the write-ahead log,
until something overwrites them.

These tests assert on the raw bytes of every file the store touches. Querying
the database cannot show this class of failure: the answer that matters is
whether the string is still on disk.
"""

import asyncio
import glob
import os
import tempfile

import pytest

from src.memory.sqlite import SQLiteMemory

CANARY = "ZZQQ-canary-payload-do-not-retain-7781"


@pytest.fixture
def store(tmp_path):
    return SQLiteMemory(config={"path": str(tmp_path / "m.db")}), str(tmp_path / "m.db")


def _files(db_path):
    return sorted(glob.glob(db_path + "*"))


def _on_disk(db_path, needle: str) -> list[str]:
    """Names of store files whose raw bytes still contain `needle`."""
    hits = []
    for f in _files(db_path):
        with open(f, "rb") as fh:
            if needle.encode() in fh.read():
                hits.append(os.path.basename(f))
    return hits


def test_forget_leaves_no_trace_of_the_content_on_disk(store):
    mem, path = store

    async def go():
        mid = await mem.store(content=CANARY, type="semantic", agent_id="t")
        assert _on_disk(path, CANARY), "precondition: the content was actually written"
        await mem.forget(mid)

    asyncio.run(go())
    assert _on_disk(path, CANARY) == [], "content survived the delete"


def test_an_edited_memory_leaves_no_trace_either(store):
    """update() logs a diff of its own, so the edit is a second copy."""
    mem, path = store

    async def go():
        mid = await mem.store(content=CANARY, type="semantic", agent_id="t")
        await mem.update(mid, agent_id="t", content=CANARY + " edited")
        await mem.forget(mid)

    asyncio.run(go())
    assert _on_disk(path, CANARY) == []


def test_the_row_the_index_and_the_changelog_all_go(store):
    mem, _ = store

    async def go():
        mid = await mem.store(content=CANARY, type="semantic", agent_id="t")
        await mem.forget(mid)
        return mid

    mid = asyncio.run(go())
    assert mem.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert mem.db.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0] == 0
    rows = mem.db.execute(
        "SELECT old_value, new_value FROM changelog WHERE record_id = ?", (mid,)).fetchall()
    assert [(r["old_value"], r["new_value"]) for r in rows] == [(None, None)], \
        "the only surviving record may carry no content"


def test_a_tombstone_still_records_that_it_happened(store):
    """Erasing the content must not erase the fact of the erasure."""
    mem, _ = store

    async def go():
        mid = await mem.store(content=CANARY, type="semantic", agent_id="t")
        await mem.forget(mid)
        return mid

    mid = asyncio.run(go())
    row = mem.db.execute(
        "SELECT action, record_id, timestamp FROM changelog WHERE record_id = ?",
        (mid,)).fetchone()
    assert row is not None, "an audit trail that forgets deletions is not one"
    assert row["action"] == "delete"
    assert row["timestamp"]


def test_forgetting_one_memory_does_not_disturb_another(store):
    mem, path = store
    keep = "KEEP-this-one-4410"

    async def go():
        gone = await mem.store(content=CANARY, type="semantic", agent_id="t")
        kept = await mem.store(content=keep, type="semantic", agent_id="t")
        await mem.forget(gone)
        return kept

    kept = asyncio.run(go())
    assert mem.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert mem.db.execute(
        "SELECT COUNT(*) FROM changelog WHERE record_id = ?", (kept,)).fetchone()[0] >= 1
    assert _on_disk(path, keep), "the surviving memory must still be there"
    assert _on_disk(path, CANARY) == []


def test_forgetting_an_unknown_id_is_harmless(store):
    mem, _ = store
    asyncio.run(mem.forget("00000000-0000-0000-0000-000000000000"))
    assert mem.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_secure_delete_is_on(store):
    """Without it, DELETE only unlinks and the text stays in the freed page."""
    mem, _ = store
    assert mem.db.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_forget_by_rowid_purges_the_changelog_under_the_real_id(store):
    """A rowid caller must not leave the uuid-keyed changelog rows behind."""
    mem, path = store

    async def go():
        mid = await mem.store(content=CANARY, type="semantic", agent_id="t")
        rowid = mem.db.execute("SELECT rowid FROM memories WHERE id = ?", (mid,)).fetchone()[0]
        await mem.forget(rowid)

    with tempfile.TemporaryDirectory():
        asyncio.run(go())
    assert _on_disk(path, CANARY) == []
