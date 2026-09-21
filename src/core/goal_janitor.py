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
  only exit from 'proposed' besides starting it.

What happens to the room on expiry depends on whether the janitor has the
vault. With it, an expired proposal goes through the same close path as any
other retirement (src/tools/goals.py `_close_goal`, reason "expired"): an
anchored proposal, which borrowed its proposer's home channel, gets the
closing notice there; a proposal with a room of its own has nothing in that
room but the cards nobody reacted to, so the room is deleted and the notice
goes to the alert channel instead. Without the vault (tests, a locked
vault) the channel gets one line saying so and how to re-propose, as before.

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
                 mention: Callable[[], str] | None = None,
                 vault=None, alert_channel: Callable[[], str] | None = None):
        cfg = cfg or {}
        self.timeout = float(cfg.get("proposal_timeout_hours", 72) or 0) * _HOUR
        remind = cfg.get("proposal_remind_hours")
        self.remind_after = (self.timeout / 2 if remind is None
                             else float(remind) * _HOUR)
        self.tick_seconds = int(cfg.get("janitor_tick_seconds", 600) or 600)
        self.connectors = connectors
        self._mention = mention or (lambda: "@here")
        self.vault = vault
        self._alert_channel = alert_channel or (lambda: "")

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
            expired = store.expire_proposal(goal["id"], now=now)
            if expired is None:
                continue
            out["expired"].append(goal["id"])
            hours = (now - goal["created_at"]) / _HOUR
            line = (f"⌛ `{goal['id']}` **{goal['title']}** expired after {hours:.0f}h "
                    f"with no decision — abandoned, {len(pending)} nomination(s) "
                    f"expired. If it is still wanted, propose it again with "
                    f"goal_create.")
            if self.vault is None:
                await self._post(goal, line)
                continue
            try:
                await self._close_expired(expired, line, hours)
            except Exception as e:
                logger.warning(f"Goal janitor: close of {goal['id']} failed: {e}")
                await self._post(goal, line)

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

    async def _room_holds_only_bot_posts(self, goal: dict) -> bool:
        """Whether the proposal's room can be deleted without losing anything a
        human wrote. A proposal room is routed (the owner answers questions
        in it), so "nothing but cards" is a claim to check, not a premise:
        one human message, a fetch that fails, or more than one page of
        history, and the answer is no."""
        from src.tools.discord_tools import _discord_get
        msgs = await _discord_get(self.vault,
                                  f"/channels/{goal['channel_id']}/messages?limit=100")
        if not isinstance(msgs, list) or len(msgs) >= 100:
            return False
        return all((m.get("author") or {}).get("bot") for m in msgs if isinstance(m, dict))

    async def _close_expired(self, goal: dict, line: str, hours: float) -> None:
        """Retire an expired proposal through the ordinary close path.

        Anchored: the closing notice lands in the borrowed home channel, and
        _close_goal never archives a borrowed channel. Own room: deleted only
        when it provably holds nothing but bot posts AND there is an alert
        channel to report the expiry in; otherwise it gets the closing notice
        and goes read-only like any other retired room. Unattended and
        irreversible is the combination this guards against: nothing a human
        did authorised the deletion, so the room must have nothing of theirs.
        """
        from src.core.base import ToolContext
        from src.tools.goals import _close_goal
        ctx = ToolContext(agent_id="system", channel_id=goal["channel_id"],
                          user_id="", vault=self.vault)
        reason = f"expired after {hours:.0f}h with no decision"
        alert = self._alert_channel()
        keep = ""
        if goal.get("anchored"):
            keep = "borrowed channel"
        elif not alert:
            keep = "no alert channel to report the expiry in"
        elif not await self._room_holds_only_bot_posts(goal):
            keep = "the room holds more than the goal's own posts"
        if keep:
            _, note = await _close_goal(ctx, goal, reason=reason)
            logger.info(f"Goal janitor: {goal['id']} expired, room kept ({keep}); {note}")
            return
        from src.tools.discord_tools import _discord_delete
        result = await _discord_delete(self.vault, f"/channels/{goal['channel_id']}")
        if not result or result.get("error"):
            detail = (result or {}).get("detail", "no Discord token")
            logger.warning(f"Goal janitor: room {goal['channel_id']} of {goal['id']} "
                           f"not deleted: {detail}")
            _, note = await _close_goal(ctx, goal, reason=reason)
            logger.info(f"Goal janitor: {goal['id']} expired, room kept; {note}")
            return
        store.log_event(goal["id"], "system", "channel_deleted",
                        f"{goal['channel_id']} (expired proposal)")
        await self._post({**goal, "channel_id": alert}, line + " Its room was removed.")

    async def _post(self, goal: dict, text: str) -> None:
        connector = self.connectors.get(goal.get("connector") or "discord")
        if not connector:
            logger.warning(f"Goal janitor: no connector for {goal['id']}, not posting")
            return
        try:
            await connector.send(goal["channel_id"], text)
        except Exception as e:
            logger.warning(f"Goal janitor: post to {goal['channel_id']} failed: {e}")
