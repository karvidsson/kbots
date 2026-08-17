#!/usr/bin/env python3
"""Export collected agent turns into training datasets.

Reads <dir>/turns.jsonl (+ rewards.jsonl) written by TrainingCollector and emits:
  - corpus.txt : role-tagged transcript text for nanoGPT (style/experiment)
  - sft.jsonl  : messages-format examples (tool_use preserved) for LoRA fine-tuning
                 a small modern model that can actually call the tools.

Rewards (👍/👎), tool errors, and degraded turns are joined per turn so you can
filter to only the good examples.

Usage:
  python scripts/export_training_data.py --dir data/training [--out data/training]
      [--agent main] [--skill tool_specialist] [--tool send_email ...]
      [--positive-only] [--successful-only] [--min-reward 0]
      [--since 2026-07-01] [--stats]

Per-tool specialist data: --tool NAME keeps only turns that actually CALLED that
tool; --skill NAME keeps only turns run under a (tool-scoped) skill. Combine with
--successful-only for clean positive examples of one tool in use. See docs/TRAINING.md.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _called_tools(turn: dict) -> set[str]:
    """Names of the tools this turn actually called (from the tool_use trace).
    Used by --tool to slice turns where a given tool was really exercised."""
    return {s.get("name") for s in (turn.get("tools") or [])
            if s.get("type") == "tool_use" and s.get("name")}


def _reward_map(rewards: list[dict]) -> dict[str, int]:
    """reply_message_id -> +1 / -1 (latest reaction wins)."""
    m: dict[str, int] = {}
    for r in rewards:
        rid = r.get("reply_message_id")
        if rid:
            m[rid] = 1 if r.get("signal") == "up" else -1
    return m


def _judge_map(judgments: list[dict]) -> dict[str, int]:
    """turn_id -> +1 / -1 from the auto-judge (latest verdict wins).
    Machine labels — weaker than human reactions; used only with --labels."""
    m: dict[str, int] = {}
    for j in judgments:
        tid = j.get("turn_id")
        if tid and j.get("verdict") in ("good", "bad"):
            m[tid] = 1 if j["verdict"] == "good" else -1
    return m


def _label(turn: dict, human_reward: int, judge_verdict: int,
           labels: str) -> tuple[str, float, str]:
    """Return (label, score, source). Human reaction dominates; judge labels are
    half magnitude (±0.5) so --min-reward 1 still means 'human 👍 only' even
    under --labels any; else derive from outcome."""
    outcome = turn.get("outcome") or {}
    if labels in ("human", "any") and human_reward:
        return ("positive" if human_reward > 0 else "negative", float(human_reward), "human")
    if labels in ("judge", "any") and judge_verdict:
        return ("positive" if judge_verdict > 0 else "negative", judge_verdict * 0.5, "judge")
    if outcome.get("degraded"):
        return ("negative", -1.0, "outcome")
    if outcome.get("tool_errors", 0) > 0:
        return ("neutral", -0.25, "outcome")
    return ("neutral", 0.0, "outcome")


def _to_messages(turn: dict) -> list[dict]:
    """Reconstruct a messages array (tool_use preserved) from a turn's tool trace."""
    messages = [{"role": "user", "content": turn.get("input", "")}]
    for step in turn.get("tools") or []:
        t = step.get("type")
        if t == "tool_use":
            messages.append({"role": "assistant", "content": "",
                             "tool_calls": [{"name": step.get("name"), "arguments": step.get("input")}]})
        elif t == "tool_result":
            messages.append({"role": "tool", "name": None,
                             "content": step.get("output", ""), "is_error": step.get("is_error", False)})
        elif t == "text" and step.get("text", "").strip():
            messages.append({"role": "assistant", "content": step["text"]})
    final = (turn.get("response") or {}).get("content", "")
    if final:
        messages.append({"role": "assistant", "content": final})
    return messages


def _to_corpus(turn: dict) -> str:
    """Readable role-tagged text block for nanoGPT."""
    parts = [f"<|user|>\n{turn.get('input','')}"]
    for step in turn.get("tools") or []:
        t = step.get("type")
        if t == "tool_use":
            parts.append(f"<|tool_call|> {step.get('name')} {json.dumps(step.get('input'), default=str)}")
        elif t == "tool_result":
            tag = "tool_error" if step.get("is_error") else "tool_result"
            parts.append(f"<|{tag}|> {step.get('output','')}")
        elif t == "text" and step.get("text", "").strip():
            parts.append(f"<|assistant|>\n{step['text']}")
    final = (turn.get("response") or {}).get("content", "")
    if final:
        parts.append(f"<|assistant|>\n{final}")
    parts.append("<|endofturn|>")
    return "\n".join(parts)


def _messages_mlx(turn: dict) -> list[dict]:
    """Clean chat messages for MLX-LM (drop helper keys; keep tool_calls / tool role)."""
    out = []
    for m in _to_messages(turn):
        if m["role"] == "tool":
            out.append({"role": "tool", "content": m.get("content", "")})
        elif m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content", ""), "tool_calls": m["tool_calls"]})
        else:
            out.append({"role": m["role"], "content": m.get("content", "")})
    return out


def _messages_openai(turn: dict) -> list[dict]:
    """Messages with OpenAI's tool-call schema (id/type/function + tool_call_id)."""
    out, n, last_id = [], 0, "call_0"
    for m in _to_messages(turn):
        if m.get("tool_calls"):
            n += 1
            last_id = f"call_{n}"
            tc = m["tool_calls"][0]
            fn = {"name": tc.get("name"), "arguments": json.dumps(tc.get("arguments") or {}, default=str)}
            out.append({"role": "assistant", "content": m.get("content") or None,
                        "tool_calls": [{"id": last_id, "type": "function", "function": fn}]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": last_id, "content": m.get("content", "")})
        else:
            out.append({"role": m["role"], "content": m.get("content", "")})
    return out


def _preference_rows(kept_with_reward):
    """KTO binary rows (from explicit 👍/👎) + DPO pairs where a prompt has both."""
    rows, by_input = [], {}
    for turn, reward in kept_with_reward:
        prompt = turn.get("input", "")
        completion = (turn.get("response") or {}).get("content", "")
        rows.append({"prompt": prompt, "completion": completion, "label": reward > 0})
        by_input.setdefault(prompt, {})["chosen" if reward > 0 else "rejected"] = completion
    pairs = [{"prompt": p, "chosen": v["chosen"], "rejected": v["rejected"]}
             for p, v in by_input.items() if "chosen" in v and "rejected" in v]
    return rows, pairs


def _split(rows: list, val_frac: float = 0.1):
    n_val = max(1, int(len(rows) * val_frac)) if len(rows) > 9 else 0
    return (rows[n_val:], rows[:n_val]) if n_val else (rows, [])


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Export agent turns to fine-tuning datasets")
    ap.add_argument("--dir", default="data/training", help="dir with turns.jsonl / rewards.jsonl")
    ap.add_argument("--out", default=None, help="output dir (default: --dir)")
    ap.add_argument("--agent", default=None, help="only this agent")
    ap.add_argument("--skill", default=None, help="only turns run under this skill")
    ap.add_argument("--tool", nargs="+", default=None,
                    help="only turns that CALLED one of these tools (per-tool specialist data)")
    ap.add_argument("--positive-only", action="store_true", help="only 👍 / non-degraded turns")
    ap.add_argument("--successful-only", action="store_true", help="drop degraded / tool-error turns")
    ap.add_argument("--min-reward", type=float, default=None, help="min reward score")
    ap.add_argument("--labels", choices=["human", "judge", "any"], default="human",
                    help="which reward sources to use (judge labels are "
                         "machine-generated and weaker; default human-only "
                         "preserves prior behavior)")
    ap.add_argument("--since", default=None, help="ISO date; only turns on/after")
    ap.add_argument("--format", nargs="+", default=["nanogpt", "sft"],
                    choices=["nanogpt", "sft", "mlx", "openai", "dpo"],
                    help="output formats (default: nanogpt sft)")
    ap.add_argument("--stats", action="store_true", help="print stats")
    args = ap.parse_args()

    src = Path(args.dir)
    out = Path(args.out or args.dir)
    out.mkdir(parents=True, exist_ok=True)
    turns = _read_jsonl(src / "turns.jsonl")
    rewards = _reward_map(_read_jsonl(src / "rewards.jsonl"))
    judgments = _judge_map(_read_jsonl(src / "judgments.jsonl"))

    # Reward-based filters over a corpus with no rewards exit 0 and say nothing
    # about why. --positive-only silently degrades to "not degraded" (every
    # clean turn is neutral, score 0), and --min-reward above 0 excludes
    # everything. Both look like a verdict on the data rather than the absence
    # of any labels at all, which sends people hunting for a fault that is not
    # there — no 👍 has ever been pressed.
    if (args.positive_only or args.min_reward is not None) and not rewards:
        where = ("no rewards.jsonl in " + str(src) if not (src / "rewards.jsonl").exists()
                 else "rewards.jsonl holds no usable rewards")
        note = ("--positive-only is falling back to outcome signals only — it keeps "
                "every non-degraded turn, which is not the same as 'good'"
                if args.positive_only else
                "--min-reward is comparing against outcome-derived scores only "
                "(0.0 for a clean turn, -0.25 with tool errors, -1.0 if degraded)")
        print(
            f"WARNING: {where} — 0 human rewards recorded.\n"
            f"         {note}.\n"
            f"         Rewards come from 👍/👎 reactions on replies. Without any,\n"
            f"         enable the judge or drop the filter (see docs/TRAINING.md).",
            file=sys.stderr,
        )

    tool_filter = set(args.tool) if args.tool else None
    kept, labels, by_agent, by_tool = [], Counter(), Counter(), Counter()
    by_source = Counter()
    for turn in turns:
        if args.agent and turn.get("agent") != args.agent:
            continue
        if args.skill and turn.get("skill") != args.skill:
            continue
        called = _called_tools(turn)
        if tool_filter and not (tool_filter & called):
            continue
        if args.since and (turn.get("ts") or "") < args.since:
            continue
        reward = rewards.get(turn.get("reply_message_id"))
        judge_v = judgments.get(turn.get("turn_id"))
        label, score, source = _label(turn, reward or 0, judge_v or 0, args.labels)
        outcome = turn.get("outcome") or {}
        if args.successful_only and (outcome.get("degraded") or outcome.get("tool_errors", 0) > 0):
            continue
        if args.positive_only and (label == "negative" or score < 0):
            continue
        if args.min_reward is not None and score < args.min_reward:
            continue
        kept.append((turn, label, score, reward, source))
        labels[label] += 1
        by_source[source] += 1
        by_agent[turn.get("agent")] += 1
        by_tool.update(called)

    fmts = set(args.format)
    written = []

    if "nanogpt" in fmts:
        p = out / "corpus.txt"
        with open(p, "w") as cf:
            for turn, *_ in kept:
                cf.write(_to_corpus(turn) + "\n\n")
        written.append(p)
    if "sft" in fmts:
        p = out / "sft.jsonl"
        _write_jsonl(p, [{"messages": _to_messages(t), "tools": t.get("tools_available") or [],
                          "agent": t.get("agent"),
                          "reward": s, "label": lbl, "label_source": src,
                          "turn_id": t.get("turn_id")}
                         for t, lbl, s, _r, src in kept])
        written.append(p)
    if "mlx" in fmts:
        train, valid = _split([{"messages": _messages_mlx(t),
                                "tools": t.get("tools_available") or []} for t, *_ in kept])
        _write_jsonl(out / "train.jsonl", train)
        written.append(out / "train.jsonl")
        if valid:
            _write_jsonl(out / "valid.jsonl", valid)
            written.append(out / "valid.jsonl")
    if "openai" in fmts:
        p = out / "openai.jsonl"
        _write_jsonl(p, [{"messages": _messages_openai(t),
                          "tools": t.get("tools_available") or []} for t, *_ in kept])
        written.append(p)
    if "dpo" in fmts:
        rewarded = [(t, r) for t, _lbl, _s, r, _src in kept if r]
        rows, pairs = _preference_rows(rewarded)
        _write_jsonl(out / "preference.jsonl", rows)   # KTO-style {prompt, completion, label}
        written.append(out / "preference.jsonl")
        if pairs:
            _write_jsonl(out / "dpo_pairs.jsonl", pairs)   # {prompt, chosen, rejected}
            written.append(out / "dpo_pairs.jsonl")

    print(f"Exported {len(kept)}/{len(turns)} turns → " + ", ".join(p.name for p in written))
    print(f"  labels: {dict(labels)} | by agent: {dict(by_agent)}")
    corpus = out / "corpus.txt"
    if corpus.exists() and corpus in written:
        toks = corpus.stat().st_size // 4
        print(f"  corpus ≈ {toks:,} tokens ({corpus.stat().st_size:,} bytes)")
        if toks < 100_000:
            print("  NOTE: tiny dataset — keep collecting; see docs/TRAINING.md.")
    if args.stats:
        print(f"  total turns on disk: {len(turns)} | rewards recorded: {len(rewards)}"
              f" | judge labels: {len(judgments)}")
        print(f"  labels by source: {dict(by_source)} (--labels {args.labels})")
        # tool-call frequency in the kept slice — shows which tools have enough
        # data to train a specialist on (see docs/TRAINING.md).
        top = ", ".join(f"{name}:{n}" for name, n in by_tool.most_common(15))
        print(f"  tool calls (kept): {top or '—'}")


if __name__ == "__main__":
    main()
