"""Reaction feedback: reply→lessons map + 👍/👎 confidence nudge."""

import pytest

from src.core import feedback_map


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


def test_record_and_get(overlay):
    feedback_map.record("msg1", "atlas", ["m1", "m2"])
    entry = feedback_map.get("msg1")
    assert entry["agent_id"] == "atlas" and entry["lessons"] == ["m1", "m2"]
    assert feedback_map.get("nope") is None


def test_record_noop_without_lessons(overlay):
    feedback_map.record("msg2", "atlas", [])
    assert feedback_map.get("msg2") is None


def test_map_is_bounded(overlay):
    for i in range(feedback_map._MAX + 20):
        feedback_map.record(f"m{i}", "a", ["x"])
    import json
    data = json.loads((overlay / "data" / "feedback_map.json").read_text())
    assert len(data) == feedback_map._MAX          # oldest pruned
    assert "m0" not in data and f"m{feedback_map._MAX + 19}" in data


# --- the handler logic (adjusting confidence via the memory backend) ---

class FakeMemory:
    def __init__(self, items):
        self.items = items

    async def get(self, mid):
        return self.items.get(mid)

    async def update(self, mid, agent_id=None, **fields):
        self.items[mid].update(fields)
        return True


class FakeMgr:
    def __init__(self, mem):
        self._mem = mem

    def _get_agent_memory(self, aid):
        return self._mem


class FakeConnector:
    def __init__(self, mgr):
        self._agent_manager = mgr


async def test_thumbs_up_promotes(overlay):
    from src.connectors.discord import DiscordBot
    mem = FakeMemory({"m1": {"id": "m1", "confidence": 0.7}})
    bot = DiscordBot.__new__(DiscordBot)          # bypass discord client init
    bot.connector = FakeConnector(FakeMgr(mem))
    feedback_map.record("r1", "atlas", ["m1"])

    await bot._handle_lesson_feedback("r1", "👍")
    assert mem.items["m1"]["confidence"] == 0.8    # +0.1

    await bot._handle_lesson_feedback("r1", "👎")
    assert mem.items["m1"]["confidence"] == 0.6    # -0.2


async def test_feedback_unknown_reply_is_safe(overlay):
    from src.connectors.discord import DiscordBot
    bot = DiscordBot.__new__(DiscordBot)
    bot.connector = FakeConnector(FakeMgr(FakeMemory({})))
    # no mapping for this message → no error, no-op
    await bot._handle_lesson_feedback("unknown", "👍")
