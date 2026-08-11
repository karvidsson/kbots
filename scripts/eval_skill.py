#!/usr/bin/env python3
"""Evaluate a skill's tool-calling accuracy against a fixtures file.

Runs each fixture input through a provider+model with the skill's rendered
prompt and its restricted ToolDefs, and checks the FIRST proposed tool call.
ONE round only — no tool is ever executed; this is side-effect-free by design.
Trap fixtures (expect_no_tool) catch over-eager calling.

Fixture JSONL, one object per line:
    {"input": "movie time", "expect_tool": "set_scene", "expect_args": {"scene": "movie"}}
    {"input": "what scenes are there?", "expect_no_tool": true}
expect_args is a SUBSET match — extra model args are fine; values are compared
after str() coercion.

Usage:
    uv run python scripts/eval_skill.py --skill scene_specialist --fixtures f.jsonl
        [--provider local] [--model qwen3.5:9b] [--param request="{input}"]
        [--min-pass 0.8]

Trap-fixture idea adapted from the fable-method project (MIT).
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load_fixtures(path: Path) -> list[dict]:
    """Parse and validate the fixtures JSONL. Raises ValueError with line numbers."""
    fixtures = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            fx = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"fixture line {i}: invalid JSON — {e}") from e
        if not (fx.get("input") or "").strip():
            raise ValueError(f"fixture line {i}: missing 'input'")
        has_tool, has_trap = bool(fx.get("expect_tool")), bool(fx.get("expect_no_tool"))
        if has_tool == has_trap:
            raise ValueError(f"fixture line {i}: need exactly one of "
                             f"'expect_tool' or 'expect_no_tool'")
        fixtures.append(fx)
    if not fixtures:
        raise ValueError(f"no fixtures in {path}")
    return fixtures


def args_match(expect: dict, got) -> bool:
    """Subset match: every expected key/value present in the proposed args.
    `got` may be a dict (Ollama native path) or a JSON string (/v1 path)."""
    if isinstance(got, str):
        try:
            got = json.loads(got or "{}")
        except json.JSONDecodeError:
            return False
    if not isinstance(got, dict):
        return False
    return all(str(got.get(k)) == str(v) for k, v in (expect or {}).items())


def score_fixture(fx: dict, resp) -> dict:
    """Score one fixture against the model's response (first proposed call only)."""
    row = {"input": fx["input"],
           "kind": "trap" if fx.get("expect_no_tool") else "call",
           "expected": "no tool" if fx.get("expect_no_tool")
                       else f"{fx.get('expect_tool')} {json.dumps(fx.get('expect_args') or {})}",
           "got_tool": None, "got_args": None, "detail": ""}
    if getattr(resp, "stop_reason", None) == "error":
        row["status"] = "ERROR"
        row["detail"] = (getattr(resp, "content", "") or "")[:120]
        return row
    calls = getattr(resp, "tool_calls", None) or []
    if calls:
        row["got_tool"] = calls[0].get("name")
        row["got_args"] = calls[0].get("arguments")
    if row["kind"] == "trap":
        row["status"] = "PASS" if not calls else "FAIL"
        if calls:
            row["detail"] = "called a tool on a no-tool input"
        return row
    if not calls:
        row["status"], row["detail"] = "FAIL", "no tool call proposed"
    elif row["got_tool"] != fx["expect_tool"]:
        row["status"], row["detail"] = "FAIL", "wrong tool"
    elif not args_match(fx.get("expect_args") or {}, row["got_args"]):
        row["status"], row["detail"] = "FAIL", "args mismatch"
    else:
        row["status"] = "PASS"
    return row


def summarize(rows: list[dict]) -> dict:
    """Rates over scored rows. ERROR rows are excluded from every denominator —
    they mean the runtime failed, not the model."""
    ok = [r for r in rows if r["status"] != "ERROR"]
    calls = [r for r in ok if r["kind"] == "call"]
    traps = [r for r in ok if r["kind"] == "trap"]
    hits = [r for r in calls if r["got_tool"] and r["detail"] != "wrong tool"]
    return {
        "total": len(rows),
        "errors": len(rows) - len(ok),
        "hit_rate": (sum(1 for r in calls if r["status"] == "PASS" or r["detail"] == "args mismatch")
                     / len(calls)) if calls else None,
        "arg_accuracy": (sum(1 for r in hits if r["status"] == "PASS") / len(hits)) if hits else None,
        "trap_pass_rate": (sum(1 for r in traps if r["status"] == "PASS") / len(traps)) if traps else None,
        "pass_rate": (sum(1 for r in ok if r["status"] == "PASS") / len(ok)) if ok else 0.0,
    }


def print_table(rows: list[dict], summary: dict) -> None:
    for r in rows:
        got = "" if r["got_tool"] is None else f" → {r['got_tool']} {r['got_args']}"
        detail = f"  [{r['detail']}]" if r["detail"] else ""
        print(f"  {r['status']:<5} {r['kind']:<4} {r['input'][:48]:<50} "
              f"expected {r['expected']}{got}{detail}")
    def pct(v):
        return "—" if v is None else f"{v * 100:.0f}%"
    print(f"\n  fixtures: {summary['total']} (errors: {summary['errors']})")
    print(f"  tool hit rate: {pct(summary['hit_rate'])} | "
          f"arg accuracy: {pct(summary['arg_accuracy'])} | "
          f"trap pass rate: {pct(summary['trap_pass_rate'])}")
    print(f"  overall pass rate: {pct(summary['pass_rate'])}")


async def run_eval(skill, fixtures: list[dict], provider, model: str | None,
                   params: dict[str, str]) -> list[dict]:
    """Mirror a real skill turn (agent_manager shape): rendered prompt as user
    content, the skill's tool list as the full scope. One completion per fixture."""
    from src.core.base import Message, MessageRole
    from src.core.skills import render_skill_prompt
    from src.core.tools import get_tools_for_agent
    tools = get_tools_for_agent(skill.tools)
    rows = []
    for fx in fixtures:
        fx_params = {**params}
        for k, v in fx_params.items():
            fx_params[k] = v.replace("{input}", fx["input"])
        rendered = render_skill_prompt(skill, fx_params)
        content = f"[Skill: {skill.name}]\n{rendered}\n\nUser message: {fx['input']}"
        kwargs = {"model": model} if model else {}
        resp = await provider.complete(
            [Message(role=MessageRole.USER, content=content)], tools=tools, **kwargs)
        rows.append(score_fixture(fx, resp))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Score a skill's tool-calling on held-out fixtures")
    ap.add_argument("--skill", required=True, help="skill name (as registered)")
    ap.add_argument("--fixtures", required=True, help="fixtures JSONL path")
    ap.add_argument("--provider", default="local", help="LLM provider (default: local)")
    ap.add_argument("--model", default=None, help="model override")
    ap.add_argument("--param", action="append", default=[],
                    help="skill parameter k=v ('{input}' expands to the fixture input); "
                         "unset params default to the fixture input")
    ap.add_argument("--min-pass", type=float, default=None,
                    help="exit 1 if overall pass rate is below this (0-1)")
    args = ap.parse_args()

    from src.core.registry import Registry
    from src.core.skills import get_all_skills, get_skill
    from src.main import load_config

    registry = Registry()
    registry.discover()
    skill = get_skill(args.skill)
    if not skill:
        print(f"Unknown skill: {args.skill}. Available: {sorted(get_all_skills())}")
        return 1
    fixtures = load_fixtures(Path(args.fixtures))

    cfg = load_config()
    defaults_llm = (cfg.get("defaults", {}) or {}).get("llm", {}) or {}
    provider = registry.create_llm_provider(args.provider, defaults_llm)
    model = args.model
    if not model and args.provider == "local":
        model = (defaults_llm.get("local", {}) or {}).get("model")

    # Unset skill params default to the fixture input — the common single-param case.
    params = {p.name: "{input}" for p in (skill.parameters or [])}
    for kv in args.param:
        k, _, v = kv.partition("=")
        params[k] = v

    print(f"Evaluating skill '{skill.name}' on {len(fixtures)} fixtures "
          f"(provider={args.provider}, model={model or 'provider default'}) — "
          f"one round, tools are never executed.\n")
    rows = asyncio.run(run_eval(skill, fixtures, provider, model, params))
    summary = summarize(rows)
    print_table(rows, summary)
    if args.min_pass is not None and summary["pass_rate"] < args.min_pass:
        print(f"\nFAIL: pass rate {summary['pass_rate']:.2f} < --min-pass {args.min_pass}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
