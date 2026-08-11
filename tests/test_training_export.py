"""export_training_data.py — join rewards, filter, emit corpus.txt + sft.jsonl."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_training_data.py"


def _write(d: Path, turns, rewards):
    (d / "turns.jsonl").write_text("\n".join(json.dumps(t) for t in turns))
    (d / "rewards.jsonl").write_text("\n".join(json.dumps(r) for r in rewards))


def _run(d: Path, *args):
    r = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(d), *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def test_corpus_and_sft(tmp_path):
    turns = [
        {"turn_id": "t1", "ts": "2026-08-01", "agent": "atlas", "reply_message_id": "m1",
         "input": "do X", "response": {"content": "done"},
         "tools": [{"type": "tool_use", "name": "read", "input": {"path": "a"}},
                   {"type": "tool_result", "output": "file contents", "is_error": False}],
         "outcome": {"degraded": False, "tool_errors": 0}},
        {"turn_id": "t2", "ts": "2026-08-01", "agent": "ledger", "reply_message_id": "m2",
         "input": "bad", "response": {"content": "oops"}, "tools": [],
         "outcome": {"degraded": True, "tool_errors": 0}},
    ]
    _write(tmp_path, turns, [{"reply_message_id": "m1", "signal": "up", "agent": "atlas"}])
    _run(tmp_path, "--stats")

    corpus = (tmp_path / "corpus.txt").read_text()
    assert "<|user|>" in corpus and "tool_call" in corpus and "done" in corpus

    sft = [json.loads(x) for x in (tmp_path / "sft.jsonl").read_text().splitlines() if x.strip()]
    assert len(sft) == 2
    j = next(x for x in sft if x["agent"] == "atlas")
    assert j["reward"] == 1.0 and j["label"] == "positive"       # 👍 joined
    roles = [m["role"] for m in j["messages"]]
    assert roles[0] == "user" and roles[-1] == "assistant" and "tool" in roles
    assert any(m.get("tool_calls") for m in j["messages"])       # tool_use preserved


_READ_SCHEMA = [{"type": "function", "function": {
    "name": "read", "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]}}}]


def _tool_turn(tid="t1", mid="m1", inp="do X", resp="done"):
    return {"turn_id": tid, "ts": "2026-08-01", "agent": "atlas", "reply_message_id": mid,
            "input": inp, "response": {"content": resp},
            "tools_available": _READ_SCHEMA,
            "tools": [{"type": "tool_use", "name": "read", "input": {"path": "a"}},
                      {"type": "tool_result", "output": "file contents", "is_error": False}],
            "outcome": {"degraded": False, "tool_errors": 0}}


def test_examples_carry_available_tools(tmp_path):
    """sft/mlx/openai examples attach the available-tool menu (train/inference parity)."""
    _write(tmp_path, [_tool_turn()], [])
    _run(tmp_path, "--format", "sft", "mlx", "openai")
    for fname in ("sft.jsonl", "train.jsonl", "openai.jsonl"):
        rec = json.loads((tmp_path / fname).read_text().splitlines()[0])
        assert rec["tools"][0]["function"]["name"] == "read", fname


def test_missing_available_tools_defaults_empty(tmp_path):
    """Old records with no tools_available still export, with an empty tools menu."""
    turn = _tool_turn()
    del turn["tools_available"]
    _write(tmp_path, [turn], [])
    _run(tmp_path, "--format", "mlx")
    rec = json.loads((tmp_path / "train.jsonl").read_text().splitlines()[0])
    assert rec["tools"] == []


def test_format_mlx(tmp_path):
    _write(tmp_path, [_tool_turn()], [])
    _run(tmp_path, "--format", "mlx")
    train = [json.loads(x) for x in (tmp_path / "train.jsonl").read_text().splitlines() if x.strip()]
    assert train and "messages" in train[0]
    tool_msg = next(m for m in train[0]["messages"] if m["role"] == "tool")
    assert "is_error" not in tool_msg and "name" not in tool_msg   # helper keys stripped
    assert any(m.get("tool_calls") for m in train[0]["messages"])   # tool_calls preserved


def test_format_openai(tmp_path):
    _write(tmp_path, [_tool_turn()], [])
    _run(tmp_path, "--format", "openai")
    msgs = json.loads((tmp_path / "openai.jsonl").read_text().splitlines()[0])["messages"]
    a = next(m for m in msgs if m.get("tool_calls"))
    call = a["tool_calls"][0]
    assert call["type"] == "function" and call["id"].startswith("call_")
    assert call["function"]["name"] == "read" and isinstance(call["function"]["arguments"], str)
    t = next(m for m in msgs if m["role"] == "tool")
    assert t["tool_call_id"] == call["id"]        # tool result linked to the call


def test_format_dpo(tmp_path):
    turns = [_tool_turn("t1", "m1", "same Q", "good"), _tool_turn("t2", "m2", "same Q", "bad")]
    _write(tmp_path, turns, [{"reply_message_id": "m1", "signal": "up"},
                             {"reply_message_id": "m2", "signal": "down"}])
    _run(tmp_path, "--format", "dpo")
    pref = [json.loads(x) for x in (tmp_path / "preference.jsonl").read_text().splitlines() if x.strip()]
    assert {r["label"] for r in pref} == {True, False}          # KTO binary labels
    pairs = [json.loads(x) for x in (tmp_path / "dpo_pairs.jsonl").read_text().splitlines() if x.strip()]
    assert pairs and pairs[0]["chosen"] == "good" and pairs[0]["rejected"] == "bad"


def test_positive_only_drops_degraded(tmp_path):
    turns = [
        {"turn_id": "t1", "agent": "a", "reply_message_id": "m1", "input": "i",
         "response": {"content": "c"}, "tools": [], "outcome": {"degraded": False, "tool_errors": 0}},
        {"turn_id": "t2", "agent": "a", "reply_message_id": "m2", "input": "i",
         "response": {"content": "c"}, "tools": [], "outcome": {"degraded": True, "tool_errors": 0}},
    ]
    _write(tmp_path, turns, [])
    _run(tmp_path, "--positive-only")
    sft = [json.loads(x) for x in (tmp_path / "sft.jsonl").read_text().splitlines() if x.strip()]
    assert len(sft) == 1 and sft[0]["turn_id"] == "t1"


def _sft(d):
    return [json.loads(x) for x in (d / "sft.jsonl").read_text().splitlines() if x.strip()]


def _turn_with(tid, mid, *, skill=None, called="read", err=False):
    """A turn tagged with a skill that CALLED a given tool (optionally with error)."""
    t = {"turn_id": tid, "ts": "2026-08-01", "agent": "atlas", "reply_message_id": mid,
         "skill": skill, "input": "do X", "response": {"content": "done"},
         "tools": [{"type": "tool_use", "name": called, "input": {"x": 1}},
                   {"type": "tool_result", "output": "ok", "is_error": err}],
         "outcome": {"degraded": False, "tool_errors": 1 if err else 0}}
    return t


def test_tool_filter_keeps_only_that_tool(tmp_path):
    _write(tmp_path, [_turn_with("t1", "m1", called="send_email"),
                      _turn_with("t2", "m2", called="read")], [])
    _run(tmp_path, "--tool", "send_email")
    sft = _sft(tmp_path)
    assert [x["turn_id"] for x in sft] == ["t1"]        # only the send_email turn


def test_skill_filter_keeps_only_that_skill(tmp_path):
    _write(tmp_path, [_turn_with("t1", "m1", skill="email_specialist"),
                      _turn_with("t2", "m2", skill="other")], [])
    _run(tmp_path, "--skill", "email_specialist")
    assert [x["turn_id"] for x in _sft(tmp_path)] == ["t1"]


def test_tool_filter_composes_with_successful_only(tmp_path):
    # two turns call send_email; one errored → --successful-only drops it
    _write(tmp_path, [_turn_with("t1", "m1", called="send_email", err=False),
                      _turn_with("t2", "m2", called="send_email", err=True)], [])
    _run(tmp_path, "--tool", "send_email", "--successful-only")
    assert [x["turn_id"] for x in _sft(tmp_path)] == ["t1"]


# --- judge labels (judgments.jsonl + --labels) ------------------------------

def _write_judgments(d: Path, judgments):
    (d / "judgments.jsonl").write_text("\n".join(json.dumps(j) for j in judgments))


def test_judge_labels_off_by_default(tmp_path):
    """Default --labels human ignores judgments.jsonl entirely."""
    _write(tmp_path, [_tool_turn()], [])
    _write_judgments(tmp_path, [{"turn_id": "t1", "verdict": "good"}])
    _run(tmp_path)
    row = _sft(tmp_path)[0]
    assert row["label"] == "neutral" and row["label_source"] == "outcome"


def test_labels_any_uses_judge(tmp_path):
    """--labels any: judge verdict labels the turn at half magnitude."""
    _write(tmp_path, [_tool_turn()], [])
    _write_judgments(tmp_path, [{"turn_id": "t1", "verdict": "good"}])
    _run(tmp_path, "--labels", "any")
    row = _sft(tmp_path)[0]
    assert row["label"] == "positive" and row["reward"] == 0.5
    assert row["label_source"] == "judge"


def test_human_beats_judge(tmp_path):
    """Human 👎 wins over a judge 'good' for the same turn under --labels any."""
    _write(tmp_path, [_tool_turn()],
           [{"reply_message_id": "m1", "signal": "down"}])
    _write_judgments(tmp_path, [{"turn_id": "t1", "verdict": "good"}])
    _run(tmp_path, "--labels", "any")
    row = _sft(tmp_path)[0]
    assert row["label"] == "negative" and row["label_source"] == "human"


def test_min_reward_1_excludes_judge_under_any(tmp_path):
    """Judge labels are ±0.5, so --min-reward 1 still means 'human 👍 only'."""
    _write(tmp_path, [_tool_turn("t1", "m1"), _tool_turn("t2", "m2")],
           [{"reply_message_id": "m2", "signal": "up"}])
    _write_judgments(tmp_path, [{"turn_id": "t1", "verdict": "good"}])
    _run(tmp_path, "--labels", "any", "--min-reward", "1")
    assert [x["turn_id"] for x in _sft(tmp_path)] == ["t2"]
