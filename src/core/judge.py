"""Turn judge — background auto-labeler for collected training turns.

A background task (sibling of the reflector in main.py). Once per interval it
reads recent unjudged turns from <training>/turns.jsonl and asks a cheap model
whether the TOOL TRACE actually accomplished the user's ask — treating the
reply's own claims as unverified evidence. Verdicts (good/bad) append to
judgments.jsonl keyed by turn_id; export_training_data.py joins them as weak
machine labels (`--labels`), with human 👍/👎 always taking precedence.
UNCLEAR verdicts write nothing. Default OFF.

Adapted from the fable-method project's "fable-judge" idea (MIT).
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from src.core import runtime_state
from src.core.base import Message, MessageRole

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are an adversarial verifier for an AI agent's completed turns. You "
    "receive the user's request, the tool calls the agent made with their "
    "results, and the agent's final reply. Treat the reply's claims as "
    "UNVERIFIED — judge only from the tool evidence: did the right tools run, "
    "did they succeed, and does the result actually satisfy the request? A "
    "confident reply backed by failed, missing, or irrelevant tool calls is "
    "BAD. A request that needed no tools, answered correctly, is GOOD.\n"
    "Reply with EXACTLY one line:\n"
    "GOOD — <short reason> | BAD — <short reason> | UNCLEAR — <short reason>\n"
    "GOOD: the work verifiably accomplished the ask. BAD: it did not (wrong, "
    "failed, or fabricated). UNCLEAR: cannot tell from the evidence. "
    "When in doubt, UNCLEAR."
)

_VERDICT_RE = re.compile(r"^\s*(GOOD|BAD|UNCLEAR)\b[\s—:–-]*(.*)$", re.IGNORECASE)
_MAX_FIELD = 1500       # truncate ask/reply/trace fields in the digest
_MAX_STEPS = 20         # cap tool-trace steps per digest


class TurnJudge:
    def __init__(self, agent_manager, training_dir, config: dict | None = None):
        self.mgr = agent_manager
        self._dir = Path(training_dir)
        cfg = config or {}
        self.enabled = cfg.get("enabled", False)
        self.provider = cfg.get("provider", "claude_code")
        self.model = cfg.get("model", "haiku")
        self.interval_h = float(cfg.get("interval_hours", 6))
        self.max_turns = int(cfg.get("max_turns_per_run", 50))
        self.min_age_s = float(cfg.get("min_age_minutes", 60)) * 60
        self.tick = float(cfg.get("tick_seconds", 900))

    # --- state ---------------------------------------------------------------

    def _due(self, now: float) -> bool:
        last = runtime_state.get_flag("judge_last_run", 0) or 0
        return (now - float(last)) >= self.interval_h * 3600

    def _work_dir(self) -> str:
        """Neutral cwd for the LLM call — no agent CLAUDE.md/.mcp.json to load."""
        d = Path(os.environ.get("KBOTS_OVERLAY", ".")) / "tmp" / "judge"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def _judged_ids(self) -> set[str]:
        """turn_ids already in judgments.jsonl — safety net if the watermark is lost."""
        path = self._dir / "judgments.jsonl"
        if not path.exists():
            return set()
        ids = set()
        for line in path.read_text().splitlines():
            try:
                tid = json.loads(line).get("turn_id")
                if tid:
                    ids.add(tid)
            except json.JSONDecodeError:
                continue
        return ids

    def _load_unjudged(self) -> list[dict]:
        path = self._dir / "turns.jsonl"
        if not path.exists():
            return []
        watermark = str(runtime_state.get_flag("judge_watermark_ts", "") or "")
        judged = self._judged_ids()
        cutoff = datetime.now(timezone.utc).timestamp() - self.min_age_s
        out = []
        for line in path.read_text().splitlines():
            try:
                turn = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = turn.get("ts") or ""
            if not turn.get("turn_id") or ts <= watermark or turn["turn_id"] in judged:
                continue
            if not (turn.get("input") or "").strip():
                continue
            if (turn.get("outcome") or {}).get("degraded"):
                continue  # already a negative outcome label; judging wastes tokens
            try:
                if datetime.fromisoformat(ts).timestamp() > cutoff:
                    continue  # too fresh — give humans first shot at reacting
            except ValueError:
                continue
            out.append(turn)
            if len(out) >= self.max_turns:
                break
        return out

    # --- judging -------------------------------------------------------------

    @staticmethod
    def _render_turn(turn: dict) -> str:
        lines = [f"USER ASK:\n{(turn.get('input') or '')[:_MAX_FIELD]}", "", "TOOL TRACE:"]
        steps = (turn.get("tools") or [])[:_MAX_STEPS]
        if not steps:
            lines.append("(no tool calls)")
        for s in steps:
            t = s.get("type")
            if t == "tool_use":
                lines.append(f"- call {s.get('name')} "
                             f"{json.dumps(s.get('input'), default=str)[:_MAX_FIELD]}")
            elif t == "tool_result":
                status = "ERROR" if s.get("is_error") else "ok"
                lines.append(f"- result ({status}): {(s.get('output') or '')[:_MAX_FIELD]}")
        reply = ((turn.get("response") or {}).get("content") or "")[:_MAX_FIELD]
        lines += ["", f"FINAL REPLY:\n{reply}"]
        return "\n".join(lines)

    @staticmethod
    def _parse_verdict(text: str) -> tuple[str, str] | None:
        m = _VERDICT_RE.match((text or "").strip().splitlines()[0] if text else "")
        if not m:
            return None
        return m.group(1).lower(), m.group(2).strip()

    def _record(self, turn: dict, verdict: str, reason: str) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "turn_id": turn.get("turn_id"),
            "agent": turn.get("agent"),
            "reply_message_id": turn.get("reply_message_id"),
            "verdict": verdict,        # "good" | "bad"
            "reason": reason[:500],
            "model": self.model,
            "source": "judge",
        }
        with open(self._dir / "judgments.jsonl", "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    async def _judge_batch(self) -> int:
        llm = self.mgr.llm_providers.get(self.provider)
        if not llm:
            logger.warning(f"Judge: provider '{self.provider}' not configured — skipping")
            return 0
        turns = self._load_unjudged()
        if not turns:
            return 0
        judged = 0
        max_ts = str(runtime_state.get_flag("judge_watermark_ts", "") or "")
        for turn in turns:
            messages = [
                Message(role=MessageRole.SYSTEM, content=_SYSTEM),
                Message(role=MessageRole.USER, content=self._render_turn(turn)),
            ]
            try:
                resp = await llm.complete(
                    messages, tools=None, project_dir=self._work_dir(),
                    model=self.model, timeout=120,
                )
            except Exception as e:
                logger.warning(f"Judge: LLM call failed — stopping run: {e}")
                break
            parsed = self._parse_verdict(resp.content or "")
            # Processed turns (incl. UNCLEAR/unparseable) advance the watermark:
            # consumed, not retried.
            max_ts = max(max_ts, turn.get("ts") or "")
            if parsed and parsed[0] in ("good", "bad"):
                self._record(turn, *parsed)
                judged += 1
        if max_ts:
            runtime_state.set_flag("judge_watermark_ts", max_ts)
        return judged

    async def run(self) -> None:
        logger.info(f"Turn judge started (provider={self.provider}, "
                    f"model={self.model}, every {self.interval_h}h)")
        while True:
            try:
                if self.enabled and self._due(time.time()):
                    n = await self._judge_batch()
                    runtime_state.set_flag("judge_last_run", time.time())
                    if n:
                        logger.info(f"Judge labeled {n} turns → judgments.jsonl")
            except Exception as e:
                logger.error(f"Judge tick failed: {e}", exc_info=True)
            await asyncio.sleep(self.tick)
