"""eval_skill.py — fixture parsing, scoring, and the stub-provider eval loop."""

import importlib.util
import json
from pathlib import Path

import pytest

from src.core.base import LLMResponse, Skill, SkillParam
from src.core.tools import tool

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_skill.py"
_spec = importlib.util.spec_from_file_location("eval_skill", SCRIPT)
eval_skill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_skill)


@tool(name="ev_scene", description="test scene tool", category="test")
async def ev_scene(ctx, scene: str = "") -> str:
    return "scene-set"


def _fixtures_file(tmp_path, lines):
    p = tmp_path / "fixtures.jsonl"
    p.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines))
    return p


def test_load_fixtures_valid_and_invalid(tmp_path):
    p = _fixtures_file(tmp_path, [
        {"input": "movie time", "expect_tool": "ev_scene", "expect_args": {"scene": "movie"}},
        {"input": "what scenes exist?", "expect_no_tool": True},
    ])
    assert len(eval_skill.load_fixtures(p)) == 2

    with pytest.raises(ValueError, match="line 1"):
        eval_skill.load_fixtures(_fixtures_file(tmp_path, [{"expect_tool": "x"}]))
    with pytest.raises(ValueError, match="exactly one"):
        eval_skill.load_fixtures(_fixtures_file(
            tmp_path, [{"input": "i", "expect_tool": "x", "expect_no_tool": True}]))
    with pytest.raises(ValueError, match="exactly one"):
        eval_skill.load_fixtures(_fixtures_file(tmp_path, [{"input": "i"}]))


def test_args_match_subset_and_string_arguments():
    assert eval_skill.args_match({"scene": "movie"}, {"scene": "movie", "extra": 1})
    assert eval_skill.args_match({"scene": "movie"}, '{"scene": "movie"}')   # /v1 JSON string
    assert eval_skill.args_match({"n": 3}, {"n": "3"})                       # str() coercion
    assert not eval_skill.args_match({"scene": "movie"}, {"scene": "away"})
    assert not eval_skill.args_match({"scene": "movie"}, "not json")
    assert not eval_skill.args_match({"scene": "movie"}, None)


def _resp(tool_calls=None, error=None):
    if error:
        return LLMResponse(content=error, stop_reason="error")
    return LLMResponse(content="", tool_calls=tool_calls)


def test_score_right_tool_wrong_args():
    fx = {"input": "movie", "expect_tool": "ev_scene", "expect_args": {"scene": "movie"}}
    row = eval_skill.score_fixture(fx, _resp([{"name": "ev_scene", "arguments": {"scene": "away"}}]))
    assert row["status"] == "FAIL" and row["detail"] == "args mismatch"
    row = eval_skill.score_fixture(fx, _resp([{"name": "other", "arguments": {}}]))
    assert row["status"] == "FAIL" and row["detail"] == "wrong tool"
    row = eval_skill.score_fixture(fx, _resp([{"name": "ev_scene", "arguments": {"scene": "movie"}}]))
    assert row["status"] == "PASS"


def test_trap_pass_and_fail():
    trap = {"input": "list scenes", "expect_no_tool": True}
    assert eval_skill.score_fixture(trap, _resp())["status"] == "PASS"
    row = eval_skill.score_fixture(trap, _resp([{"name": "ev_scene", "arguments": {}}]))
    assert row["status"] == "FAIL" and "no-tool input" in row["detail"]


def test_error_rows_and_summary_rates():
    fx = {"input": "movie", "expect_tool": "ev_scene", "expect_args": {"scene": "movie"}}
    rows = [
        eval_skill.score_fixture(fx, _resp([{"name": "ev_scene", "arguments": {"scene": "movie"}}])),
        eval_skill.score_fixture(fx, _resp([{"name": "ev_scene", "arguments": {"scene": "away"}}])),
        eval_skill.score_fixture({"input": "q", "expect_no_tool": True}, _resp()),
        eval_skill.score_fixture(fx, _resp(error="Local model request failed")),
    ]
    s = eval_skill.summarize(rows)
    assert s["total"] == 4 and s["errors"] == 1        # ERROR excluded from rates
    assert s["hit_rate"] == 1.0                        # right tool both times
    assert s["arg_accuracy"] == 0.5                    # args right once
    assert s["trap_pass_rate"] == 1.0
    assert s["pass_rate"] == pytest.approx(2 / 3)


async def test_run_eval_with_stub_provider():
    skill = Skill(name="scene_toy", description="d",
                  prompt="Set the scene for: {request}. Valid: movie, goodnight.",
                  tools=["ev_scene"],
                  parameters=[SkillParam(name="request", required=True)])
    calls = []

    class StubProvider:
        async def complete(self, messages, tools=None, **kw):
            calls.append({"messages": messages, "tools": tools})
            if "User message: movie time" in messages[0].content:
                return _resp([{"name": "ev_scene", "arguments": {"scene": "movie"}}])
            return _resp()

    fixtures = [
        {"input": "movie time", "expect_tool": "ev_scene", "expect_args": {"scene": "movie"}},
        {"input": "which scenes are there?", "expect_no_tool": True},
    ]
    rows = await eval_skill.run_eval(skill, fixtures, StubProvider(), None,
                                     {"request": "{input}"})
    assert [r["status"] for r in rows] == ["PASS", "PASS"]
    # parity with a real skill turn: rendered prompt + [Skill: ...] header + ToolDefs
    content = calls[0]["messages"][0].content
    assert content.startswith("[Skill: scene_toy]")
    assert "Set the scene for: movie time." in content
    assert "User message: movie time" in content
    assert [t.name for t in calls[0]["tools"]] == ["ev_scene"]
