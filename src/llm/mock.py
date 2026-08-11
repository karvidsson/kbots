"""Mock LLM provider — deterministic responses, zero quota, no subprocess.

For dev/test runs (scripts/dev.py chat --mock) and pipeline tests: exercises
routing, sessions, memory, and connectors without calling Claude.

Config options (under the agent's llm block or defaults.llm):
  responses: [ "canned reply 1", "canned reply 2", ... ]  # cycled in order
Without `responses`, echoes the last user message.
"""

import logging

from src.core.base import LLMProvider, LLMResponse, Message

logger = logging.getLogger(__name__)


class MockProvider(LLMProvider):
    """Echo/canned-response provider for development and tests."""
    name = "mock"

    def __init__(self, config: dict):
        super().__init__(config)
        self._call_count = 0

    async def complete(
        self,
        messages: list[Message],
        tools=None,
        stream: bool = False,
        **kwargs,
    ) -> LLMResponse:
        self._call_count += 1

        last_user = ""
        for m in reversed(messages):
            role = getattr(m.role, "value", m.role)
            if role == "user":
                last_user = m.content
                break

        responses = self.config.get("responses") or []
        if responses:
            content = responses[(self._call_count - 1) % len(responses)]
        else:
            # Echo only the actual user text (strip injected context blocks)
            text = last_user.rsplit("\n\n", 1)[-1] if "\n\n" in last_user else last_user
            content = f"[mock] echo: {text}"

        return LLMResponse(
            content=content,
            tool_calls=None,
            tokens_used=0,
            model="mock",
            stop_reason="end",
            session_id=kwargs.get("session_id") or f"mock-session-{self._call_count}",
        )
