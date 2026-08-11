"""TurnJudge auto-labeling: verdict parsing, watermark, idempotence, min-age."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.core import runtime_state
from src.core.base import LLMResponse
from src.core.judge import TurnJudge


class FakeLLM:
    def __init__(self, content):
        self.calls = []
        self._content = content

    async def complete(self, messages, tools=None, **kw):
        self.calls.append({"messages": messages, "tools": tools, **kw})
        return LLMResponse(content=self._content)


class FakeMgr:
    def __init__(self, llm):
        self.llm_providers = {"claude_code": llm} if llm else {}


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


def _turn(turn_id="t1", age_hours=2.0, **over):
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    turn = {
        "turn_id": turn_id, "ts": ts, "agent": "bot", "reply_message_id": "m1",
        "input": "turn off the office lights",
        "response": {"content": "Done — office lights off."},
        "tools": [
            {"type": "tool_use", "name": "shelly_switch",
             "input": {"device": "office_light", "state": "off"}},
            {"type": "tool_result", "output": "office_light → OFF", "is_error": False},
        ],
        "outcome": {"degraded": False, "tool_errors": 0},
    }
    turn.update(over)
    return turn


def _write_turns(training_dir, turns):
    training_dir.mkdir(parents=True, exist_ok=True)
    with open(training_dir / "turns.jsonl", "w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


async def test_judge_writes_good_label(overlay, tmp_path):
    tdir = tmp_path / "training"
    _write_turns(tdir, [_turn()])
    llm = FakeLLM("GOOD — trace matches the ask")
    j = TurnJudge(FakeMgr(llm), tdir, {"enabled": True})
    assert await j._judge_batch() == 1
    rows = [json.loads(x) for x in (tdir / "judgments.jsonl").read_text().splitlines()]
    assert rows[0]["turn_id"] == "t1" and rows[0]["verdict"] == "good"
    assert rows[0]["source"] == "judge" and "trace matches" in rows[0]["reason"]
    # single no-tools call on the cheap model in a neutral cwd
    assert llm.calls[0]["model"] == "haiku" and llm.calls[0]["tools"] is None


async def test_judge_unclear_writes_nothing_but_advances_watermark(overlay, tmp_path):
    tdir = tmp_path / "training"
    turn = _turn()
    _write_turns(tdir, [turn])
    j = TurnJudge(FakeMgr(FakeLLM("UNCLEAR — cannot tell")), tdir, {"enabled": True})
    assert await j._judge_batch() == 0
    assert not (tdir / "judgments.jsonl").exists()
    # consumed, not retried: watermark passed the turn
    assert runtime_state.get_flag("judge_watermark_ts") == turn["ts"]
    assert await j._judge_batch() == 0            # second run sees nothing new


async def test_judge_skips_already_judged(overlay, tmp_path):
    tdir = tmp_path / "training"
    _write_turns(tdir, [_turn()])
    tdir.joinpath("judgments.jsonl").write_text(
        json.dumps({"turn_id": "t1", "verdict": "good"}) + "\n")
    llm = FakeLLM("GOOD — x")
    j = TurnJudge(FakeMgr(llm), tdir, {"enabled": True})
    assert await j._judge_batch() == 0            # no watermark, but id already judged
    assert llm.calls == []


async def test_judge_respects_min_age_and_degraded(overlay, tmp_path):
    tdir = tmp_path / "training"
    _write_turns(tdir, [
        _turn(turn_id="fresh", age_hours=0.1),                        # too fresh
        _turn(turn_id="broken", outcome={"degraded": True}),          # already negative
    ])
    llm = FakeLLM("GOOD — x")
    j = TurnJudge(FakeMgr(llm), tdir, {"enabled": True, "min_age_minutes": 60})
    assert await j._judge_batch() == 0
    assert llm.calls == []


def test_parse_verdict():
    p = TurnJudge._parse_verdict
    assert p("GOOD — did the thing") == ("good", "did the thing")
    assert p("bad: wrong device") == ("bad", "wrong device")
    assert p("Unclear - no evidence\nsecond line") == ("unclear", "no evidence")
    assert p("The work looks fine to me") is None
    assert p("") is None
