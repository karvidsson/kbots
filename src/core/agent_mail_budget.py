"""Loop guard for fire-and-forget inter-agent messages.

The old guard was a hop counter that grew on every send and only reset when a
human spoke. It could not tell a loop from two agents working together: a
production thread between two agents stalled at its fourth message eight
times in two days, and the work carried on through status files and repeated
requests instead.

What a send loop actually looks like is a pair of agents trading messages
without end, or one agent re-sending the same thing. So this budgets each
pair of agents over a rolling hour and drops exact repeats, and leaves the
hop counter to ask_agent, where one agent blocks waiting on another and the
nesting is real.
"""

import time
from collections import deque

PAIR_LIMIT = 12          # messages either way between two agents, per window
PAIR_WINDOW = 3600.0     # seconds
REPEAT_WINDOW = 600.0    # an identical message inside this is a repeat


def _norm(content: str) -> str:
    return " ".join(content.split()).lower()


class PairBudget:
    def __init__(self, limit: int = PAIR_LIMIT, window: float = PAIR_WINDOW,
                 repeat_window: float = REPEAT_WINDOW):
        self.limit, self.window, self.repeat_window = limit, window, repeat_window
        self._sent: dict[frozenset, deque] = {}
        self._last: dict[tuple[str, str], tuple[str, float]] = {}

    def admit(self, sender: str, target: str, content: str, now: float | None = None) -> str:
        """"" to deliver, otherwise the reason it was refused (said to the sender)."""
        now = time.time() if now is None else now
        body = _norm(content)
        last = self._last.get((sender, target))
        if last and last[0] == body and now - last[1] < self.repeat_window:
            return (f"Not delivered: you sent {target} this exact message "
                    f"{int(now - last[1])}s ago. It already has it.")
        pair = frozenset((sender, target))
        sent = self._sent.setdefault(pair, deque())
        while sent and now - sent[0] >= self.window:
            sent.popleft()
        if len(sent) >= self.limit:
            wait = int(self.window - (now - sent[0])) + 1
            return (f"Not delivered: {sender} and {target} have exchanged {self.limit} "
                    f"messages in the last hour, the limit that stops two agents looping. "
                    f"Put what is left in one message after {wait}s, or write it to a "
                    f"shared file and say where.")
        sent.append(now)
        self._last[(sender, target)] = (body, now)
        return ""
