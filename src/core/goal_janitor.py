"""Goal janitor — the nudge and the timeout a proposal never had.

A HITL request reminds once and times out (src/core/hitl_notify.py). A goal
proposal did neither: the kickoff card and its nomination cards sat in the
channel waiting for a ✅, and if the human never looked, the goal stayed
'proposed' forever with nominations pending and an owner waiting on a
decision nobody knew was owed.

This background task (sibling of the browser janitor in main.py) reads the
goal store on a slow tick and, per proposal:

- at `proposal_remind_hours` (default half the timeout) posts ONE reminder in
  the goal's channel, addressed to the escalation user;
- at `proposal_timeout_hours` (default 72) expires it: pending nominations
  become 'expired' (nobody said no) and the goal is abandoned, which is the
  only exit from 'proposed' besides starting it. The channel gets one line
  saying so and how to re-propose.

`proposal_timeout_hours: 0` disables both. All of it is a store read plus a
connector send, so a broken tick logs and the next tick retries.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from src.core import goals as store

logger = logging.getLogger(__name__)

_HOUR = 3600.0


class GoalJanitor:
    def __init__(self, cfg: dict, connectors: dict,
                 mention: Callable[[], str] | None = None):
        cfg = cfg or {}
        self.timeout = float(cfg.get("proposal_timeout_hours", 72) or 0) * _HOUR
        remind = cfg.get("proposal_remind_hours")
        self.remind_after = (self.timeout / 2 if remind is None
                             else float(remind) * _HOUR)
        self.tick_seconds = int(cfg.get("janitor_tick_seconds", 600) or 600)
        self.connectors = connectors
        self._mention = mention or (lambda: "@here")

    @property
    def enabled(self) -> bool:
        return self.timeout > 0

    @property
    def reminds(self) -> bool:
        return 0 < self.remind_after < self.timeout

    async def run(self) -> None:
        logger.info(
            f"Goal janitor: proposals expire after {self.timeout / _HOUR:g}h"
            + (f", reminder at {self.remind_after / _HOUR:g}h" if self.reminds
               else ", no reminder"))
        while True:
            try:
                await self.tick()
            except Exception as e:
                logger.warning(f"Goal janitor tick failed: {e}")
            await asyncio.sleep(self.tick_seconds)

    async def tick(self, now: float | None = None) -> dict[str, list[str]]:
        """One pass. Returns {"expired": [...ids], "reminded": [...ids]}."""
        now = time.time() if now is None else now
        out: dict[str, list[str]] = {"expired": [], "reminded": []}
        if not self.enabled:
            return out

        for goal in store.stale_proposals(self.timeout, now=now):
            pending = store.list_nominations(goal["id"], "pending")
            if store.expire_proposal(goal["id"], now=now) is None:
                continue
            out["expired"].append(goal["id"])
            hours = (now - goal["created_at"]) / _HOUR
            await self._post(goal, (
                f"⌛ `{goal['id']}` **{goal['title']}** expired after {hours:.0f}h "
                f"with no decision — abandoned, {len(pending)} nomination(s) "
                f"expired. If it is still wanted, propose it again with "
                f"goal_create."))

        if not self.reminds:
            return out
        for goal in store.stale_proposals(self.remind_after, reminded=False, now=now):
            store.mark_proposal_reminded(goal["id"], now=now)
            out["reminded"].append(goal["id"])
            pending = store.list_nominations(goal["id"], "pending")
            waited = (now - goal["created_at"]) / _HOUR
            left = max(0.0, (goal["created_at"] + self.timeout - now) / _HOUR)
            await self._post(goal, (
                f"⏳ `{goal['id']}` **{goal['title']}** has waited {waited:.0f}h "
                f"for a decision ({len(pending)} nomination(s) pending). React ✅ "
                f"on the kickoff card to start it, ❌ on a nominee to keep them "
                f"off. It expires in {left:.0f}h. {self._mention()}"))
        return out

    async def _post(self, goal: dict, text: str) -> None:
        connector = self.connectors.get(goal.get("connector") or "discord")
        if not connector:
            logger.warning(f"Goal janitor: no connector for {goal['id']}, not posting")
            return
        try:
            await connector.send(goal["channel_id"], text)
        except Exception as e:
            logger.warning(f"Goal janitor: post to {goal['channel_id']} failed: {e}")
