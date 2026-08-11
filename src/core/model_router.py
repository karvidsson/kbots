"""Model-tier router — a small local model triages requests before Claude Code.

Opt-in per agent (llm.router.enabled). Each incoming message is classified by a
tiny local model (via the `local` provider — Ollama/LM Studio); clearly-simple
requests are answered by a local workhorse model, everything else goes to the
agent's configured provider (Claude Code). Saves subscription usage without
sacrificing quality:

  QUALITY-FIRST — the router only keeps a request local when it is confidently
  simple. Attachments, code, long messages, scheduled tasks, classifier errors,
  low confidence, or a missing/down local runtime all escalate to Claude.

Config (per-agent llm.router, keys fall back to defaults.llm.router):
  router:
    enabled: true
    router_model: qwen3.5:2b   # tiny classifier (always loaded)
    local_model: qwen3.5:9b    # workhorse for simple requests
    confidence: 0.75           # min classifier confidence to stay local
    max_chars: 600             # longer messages escalate
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from src.core.base import Message, MessageRole

logger = logging.getLogger(__name__)

_CLASSIFIER_PROMPT = """You route incoming chat messages for an AI assistant. \
Classify the message as "simple" or "complex".

simple = greetings, casual chat, thanks, quick factual questions answerable from \
general knowledge, translating/rewording/summarizing text that is INCLUDED in the \
message itself, short opinions.

complex = ANYTHING that needs tools or fresh data (web, news, prices, weather, \
email, calendar, files, schedules/reminders), multi-step work, writing or fixing \
code, questions about this platform/system/agents themselves, ANY instruction or \
directive for the assistant to DO, build, change, decide, or take over something \
(even a short one like "you handle it" or "go ahead"), or anything you are \
not sure about.

Reply with ONLY this JSON, nothing else:
{"route": "simple" | "complex", "confidence": 0.0-1.0}"""

_CLASSIFY_TIMEOUT = 30  # covers a cold model load; warm classify is a few seconds

# Work-directive vocabulary that must never be answered by the local workhorse:
# it has no CLI builtins and no repo access, so "build/deploy/fix …" turns become
# confident promises with no execution behind them (observed in production).
# English + the Swedish equivalents this deployment sees. Deterministic — runs
# before the classifier, and a miss still falls through to it (quality-first).
_ACTION_RE = re.compile(
    r"\b(build|implement|deploy|ship|fix|patch|commit|push|merge|rebase|refactor|"
    r"rewrite|debug|install|configure|migrate|release|repo|repository|branch|"
    r"codebase|pull request|bygg|bygga|fixa|implementera|deploya|koda)\b|\bPR\b",
    re.IGNORECASE,
)


@dataclass
class RouteDecision:
    target: str  # "local" | "claude"
    reason: str


def _claude(reason: str) -> RouteDecision:
    return RouteDecision("claude", reason)


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group(0)) if m else {}


class ModelRouter:
    """Classifies messages and applies the routing decision to a session."""

    def __init__(self, llm_providers: dict):
        self._providers = llm_providers

    async def route(self, content: str, has_attachments: bool, cfg: dict,
                    session=None) -> RouteDecision:
        """Decide where this message should be handled. Pure — no session mutation."""
        # --- deterministic escalation rules (fast path, no model call) ---
        # An active Claude CLI session means an in-flight agentic thread (tool
        # use, pending work). Routing a mid-task follow-up ("approve", "yes,
        # do it") to the toolless local model makes it role-play the work —
        # observed live as invented reboots/relays/avatar uploads. Sticky by
        # default; opt out with router.sticky_claude: false.
        if (cfg.get("sticky_claude", True) and session is not None
                and getattr(session, "cli_session_id", None)):
            return _claude("active claude thread")
        if has_attachments:
            return _claude("attachments")
        if content.startswith("⏰"):
            return _claude("scheduled task")
        if "```" in content:
            return _claude("code block")
        if _ACTION_RE.search(content):
            return _claude("action directive")
        if len(content) > int(cfg.get("max_chars", 600)):
            return _claude("long message")

        local = self._providers.get("local")
        if local is None:
            return _claude("no local provider")

        # --- classify with the tiny router model ---
        # think=False + force_json: on Ollama this uses the native /api/chat with
        # thinking disabled and constrained-JSON decoding — reasoning models
        # (qwen3.5) otherwise think past any sane timeout on the /v1 endpoint.
        try:
            resp = await asyncio.wait_for(
                local.complete(
                    [Message(role=MessageRole.SYSTEM, content=_CLASSIFIER_PROMPT),
                     Message(role=MessageRole.USER, content=content)],
                    model=cfg.get("router_model") or "",
                    think=False, force_json=True,
                ),
                timeout=float(cfg.get("classify_timeout", _CLASSIFY_TIMEOUT)),
            )
            if resp.stop_reason == "error":
                return _claude(f"classifier error: {resp.content[:80]}")
            data = _extract_json(resp.content)
            route = data.get("route", "complex")
            conf = float(data.get("confidence", 0.0))
        except Exception as e:  # timeout, junk JSON, runtime down — fail open to quality
            return _claude(f"classifier failed: {type(e).__name__}: {e}")

        threshold = float(cfg.get("confidence", 0.75))
        if route == "simple" and conf >= threshold:
            return RouteDecision("local", f"simple @{conf:.2f}")
        return _claude(f"{route} @{conf:.2f}")

    @staticmethod
    def apply(decision: RouteDecision, session) -> bool:
        """Apply a decision to the session; returns True when handling locally.

        Context-safety on switching: local turns are stored in SQLite but NOT in
        Claude Code's CLI session transcript — so when a conversation escalates
        back to Claude after local turns, the stale CLI session is cleared and
        Claude rebuilds context from stored history instead of resuming a
        transcript that is missing those turns.
        """
        if decision.target == "local":
            session.routed_local = True
            return True
        if getattr(session, "routed_local", False) and session.cli_session_id:
            session.cli_session_id = None
        session.routed_local = False
        return False
