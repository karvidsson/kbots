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


# --- reward attribution (2026-08-22) ---
#
# rewards.jsonl on the live store had 2 rows and both said agent=null. The
# caller resolves the agent through feedback_map, which is only written for
# replies that recalled a lesson: 32 of 1163 turns. Export survives it, because
# it joins to the turn on reply_message_id, but the file is unreadable on its
# own and no per-agent reward count is possible without redoing that join.

def _collector(tmp_path):
    from src.core.training_collector import TrainingCollector
    return TrainingCollector(tmp_path, include_tool_trace=False)


def _turn(tc, agent, reply_id):
    import json
    with open(tc._turns_path, "a") as fh:
        fh.write(json.dumps({"agent": agent, "reply_message_id": reply_id}) + "\n")


def _rewards(tc):
    import json
    tc.close()
    return [json.loads(line) for line in
            open(tc._rewards_path).read().splitlines() if line.strip()]


def test_a_reward_is_attributed_from_the_turn_when_the_caller_has_no_agent(tmp_path):
    tc = _collector(tmp_path)
    _turn(tc, "engineer", "999")
    tc.record_reward("999", None, "up", user_id="owner")
    assert _rewards(tc)[0]["agent"] == "engineer"


def test_an_agent_the_caller_already_knows_is_not_overridden(tmp_path):
    """feedback_map is the better source when it has an answer: it was written
    at send time by the agent itself.
    """
    tc = _collector(tmp_path)
    _turn(tc, "engineer", "999")
    tc.record_reward("999", "ledger", "up")
    assert _rewards(tc)[0]["agent"] == "ledger"


def test_an_unknown_reply_still_records_the_reward(tmp_path):
    """A reaction on something older than the lookback window, or on a message
    the collector never saw, is still a real signal. Dropping it to avoid a
    null would lose the thing being measured.
    """
    tc = _collector(tmp_path)
    tc.record_reward("nope", None, "down")
    row = _rewards(tc)[0]
    assert row["agent"] is None and row["signal"] == "down"


def test_the_lookup_reads_only_the_tail_of_a_large_file(tmp_path):
    """turns.jsonl is 40MB on the live store. Reading it forwards to answer a
    thumbs-up would cost more than the signal is worth.
    """
    from src.core import training_collector

    tc = _collector(tmp_path)
    _turn(tc, "engineer", "old")
    for i in range(training_collector._REWARD_LOOKBACK + 50):
        _turn(tc, "someone", f"filler{i}")
    tc.record_reward("old", None, "up")
    assert _rewards(tc)[0]["agent"] is None, "scanned further back than the window"


def test_the_most_recent_turn_wins_for_a_repeated_reply_id(tmp_path):
    tc = _collector(tmp_path)
    _turn(tc, "old-agent", "555")
    _turn(tc, "new-agent", "555")
    tc.record_reward("555", None, "up")
    assert _rewards(tc)[0]["agent"] == "new-agent"
