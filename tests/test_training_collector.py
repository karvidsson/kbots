"""TrainingCollector — record shape, redaction, transcript parsing + per-turn slicing."""

import json
import types

from src.core.base import ToolDef, ToolParam
from src.core.training_collector import TrainingCollector


def _objs(cli="sess1"):
    session = types.SimpleNamespace(id="atlas:chan", cli_session_id=cli, project_dir="/tmp/agent")
    message = types.SimpleNamespace(connector="discord", channel_id="chan", user_id="u", skill=None)
    response = types.SimpleNamespace(content="hello", model="sonnet", tokens_used=42, stop_reason="end_turn")
    return session, message, response


def test_records_turn(tmp_path):
    tc = TrainingCollector(tmp_path, include_tool_trace=False)
    s, m, r = _objs()
    tc.record_turn(agent_id="atlas", session=s, message=m, user_content="hi there",
                   response=r, reply_message_id="123")
    tc.close()
    rec = json.loads((tmp_path / "turns.jsonl").read_text().splitlines()[0])
    assert rec["agent"] == "atlas" and rec["input"] == "hi there"
    assert rec["response"]["content"] == "hello" and rec["response"]["tokens_used"] == 42
    assert rec["reply_message_id"] == "123" and rec["tools"] == []
    assert rec["outcome"]["tool_calls"] == 0
    assert rec["tools_available"] == []      # no tools offered → empty menu


def test_records_available_tool_schemas(tmp_path):
    tc = TrainingCollector(tmp_path, include_tool_trace=False)
    s, m, r = _objs()
    tool = ToolDef(name="get_weather", description="Look up weather",
                   parameters=[ToolParam(name="city", type="string", description="City",
                                         required=True)],
                   func=lambda: None, category="demo", hitl=False)
    tc.record_turn(agent_id="atlas", session=s, message=m, user_content="weather?",
                   response=r, reply_message_id="123", tools_available=[tool])
    tc.close()
    rec = json.loads((tmp_path / "turns.jsonl").read_text().splitlines()[0])
    # OpenAI function-schema shape — the same menu the local provider sends at inference
    fn = rec["tools_available"][0]["function"]
    assert fn["name"] == "get_weather"
    assert fn["parameters"]["properties"]["city"]["type"] == "string"
    assert fn["parameters"]["required"] == ["city"]


def test_reward_written(tmp_path):
    tc = TrainingCollector(tmp_path)
    tc.record_reward("123", "atlas", "up", "u1")
    tc.close()
    rec = json.loads((tmp_path / "rewards.jsonl").read_text().splitlines()[0])
    assert rec["reply_message_id"] == "123" and rec["signal"] == "up" and rec["agent"] == "atlas"


def test_never_raises_on_bad_input(tmp_path):
    tc = TrainingCollector(tmp_path)
    tc.record_turn(agent_id="x", session=object(), message=object(), user_content=None, response=object())
    tc.record_reward(None, "x", "up")   # no reply id → skipped
    tc.close()


def test_extracts_and_slices_tool_trace(tmp_path, monkeypatch):
    transcript = tmp_path / "sess.jsonl"
    monkeypatch.setattr("src.core.training_collector.session_transcript_path",
                        lambda cwd, sid: transcript)
    tc = TrainingCollector(tmp_path)
    transcript.write_text("\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "let me search"},
            {"type": "tool_use", "name": "web_search", "input": {"query": "x", "api_key": "SECRET"}}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "result text"}]}]}}),
    ]))
    steps = tc._extract_tools("/tmp/agent", "sess")
    assert [s["type"] for s in steps] == ["text", "tool_use", "tool_result"]
    assert steps[1]["name"] == "web_search"
    assert steps[1]["input"]["api_key"] == "[REDACTED]"      # secret redacted
    assert steps[2]["output"] == "result text"

    # append one more line → per-turn slicing returns only the NEW step
    with open(transcript, "a") as f:
        f.write("\n" + json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "read", "input": {"path": "a"}}]}}))
    steps2 = tc._extract_tools("/tmp/agent", "sess")
    assert [s["type"] for s in steps2] == ["tool_use"] and steps2[0]["name"] == "read"
