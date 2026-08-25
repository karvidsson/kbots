"""SQLiteMemory.list_since — the reflector's graph-extraction cursor walk."""

from src.memory.sqlite import SQLiteMemory


async def test_list_since_walks_all_rows_exactly_once(tmp_path):
    mem = SQLiteMemory({"path": str(tmp_path / "m.db")})
    try:
        stored = set()
        for i in range(5):
            stored.add(await mem.store(f"fact {i}", "semantic", agent_id="a"))

        # limit=1 forces the cursor through same-second timestamps (all five
        # rows share one CURRENT_TIMESTAMP) — the id tiebreak must make the
        # walk lossless and duplicate-free.
        seen, cursor = [], None
        while True:
            batch = await mem.list_since("a", since=cursor, limit=1)
            if not batch:
                break
            seen.extend(m["id"] for m in batch)
            last = batch[-1]
            cursor = (str(last["updated_at"]), str(last["id"]))
        assert len(seen) == 5 and set(seen) == stored
    finally:
        mem.close()


async def test_list_since_scope_and_archive_filtering(tmp_path):
    mem = SQLiteMemory({"path": str(tmp_path / "m.db")})
    try:
        await mem.store("mine", "semantic", agent_id="a")
        await mem.store("someone else's", "semantic", agent_id="b")
        await mem.store("someone else's secret", "semantic", agent_id="b",
                        scope="private:b")
        await mem.store("shared", "semantic", agent_id="b", scope="global")
        rows = await mem.list_since("a", limit=10)
        contents = {m["content"] for m in rows}
        # Another agent's ordinary memory is readable; its private one is not.
        assert contents == {"mine", "someone else's", "shared"}
    finally:
        mem.close()
