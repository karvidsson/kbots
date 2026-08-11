"""Scheduler loop — fires due scheduled tasks into the agent pipeline.

Runs as a background task in the engine. Each tick it asks schedules.py for due
schedules and re-invokes the owning agent with the stored instruction, exactly
like a user message — so the agent runs it with full tools/memory and replies
in the schedule's channel. The reply goes out via the agent's own connector.

If a schedules oversight channel is configured, the scheduler also keeps a board
there: a card when a task is first seen (react ❌ to cancel), a line when one
fires, and a line when one finishes (expired / max_runs / one-off done).
"""

import asyncio
import logging
import time
from datetime import datetime

from src.core import runtime_state
from src.core import schedules as sched
from src.core.base import IncomingMessage

logger = logging.getLogger(__name__)


def _timing(s: dict) -> str:
    st = s.get("spec_type")
    if st == "cron":
        return f"cron `{s['spec']}`"
    if st == "every":
        return f"every {int(s['spec']) // 60}min"
    return "once @ " + datetime.fromtimestamp(float(s["spec"])).strftime("%Y-%m-%d %H:%M")


class Scheduler:
    def __init__(self, agent_manager, tick: float = 30.0,
                 schedules_channel: str = "", schedules_bot: str = ""):
        self.agent_manager = agent_manager
        self.tick = tick
        self.schedules_channel = schedules_channel
        self.schedules_bot = schedules_bot

    def _board_channel(self) -> str:
        """Live oversight channel — a runtime override (set_schedule_board) wins
        over the config value, so the board can be turned on/off without a restart."""
        ch = runtime_state.get_flag("schedules_channel", None)
        return self.schedules_channel if ch is None else ch

    def _board_bot(self) -> str:
        b = runtime_state.get_flag("schedules_bot", None)
        return self.schedules_bot if b is None else b

    async def _post(self, content: str) -> None:
        """Post to the oversight channel (no-op if unconfigured)."""
        channel = self._board_channel()
        if not channel:
            return
        connector = (getattr(self.agent_manager, "connectors", {}) or {}).get("discord")
        if not connector:
            return
        try:
            await connector.send(channel, content,
                                 bot_account=self._board_bot() or None)
        except Exception as e:
            logger.error(f"Schedule board post failed: {e}", exc_info=True)

    async def _reconcile_board(self) -> None:
        """Announce newly-seen schedules and post closures for finished ones."""
        for s in sched.list_schedules():
            enabled = s.get("enabled", True)
            if enabled and not s.get("announced"):
                await self._post(
                    f"⏰ Scheduled `{s['id']}` · agent `{s.get('agent_id')}` · {_timing(s)}\n"
                    f"> {s.get('instruction', '')}\n"
                    f"React ❌ on this message to cancel it.")
                sched.set_fields(s["id"], announced=True)
            elif not enabled and s.get("announced") and not s.get("closed"):
                reason = "one-off done" if s.get("spec_type") == "once" else "stopped"
                await self._post(f"🛑 `{s['id']}` finished ({reason}).")
                sched.set_fields(s["id"], closed=True)

    async def _fire(self, schedule: dict) -> None:
        mgr = self.agent_manager
        agent_id = schedule["agent_id"]
        if agent_id not in getattr(mgr, "agent_configs", {}):
            logger.warning(f"Schedule {schedule['id']} → unknown agent '{agent_id}'")
            return

        # Tool-direct action: execute the bound tool deterministically — no
        # LLM turn at all (HITL/rate-limits/audit still apply via dispatch).
        if schedule.get("action"):
            from src.core.actions import run_tool_action
            logger.info(f"Schedule {schedule['id']} firing action for {agent_id}")
            await self._post(f"▶️ `{schedule['id']}` ran (action, agent `{agent_id}`).")
            asyncio.create_task(
                run_tool_action(mgr, schedule, "schedule"),
                name=f"schedule-{schedule['id']}")
            return

        routing = mgr.agent_configs[agent_id].get("routing", {})
        account = (routing.get(schedule["connector"], {}) or {}).get("account")
        msg = IncomingMessage(
            connector=schedule["connector"],
            channel_id=schedule["channel_id"],
            user_id=schedule.get("created_by") or "",
            user_name="scheduler",
            content=f"⏰ Scheduled task: {schedule['instruction']}",
            bot_account=account,
            # A schedule may fire a registered skill — the skill's own llm
            # pin / tool scoping (PR feat/skill-llm-pinning) then apply.
            skill=schedule.get("skill") or None,
            skill_params=schedule.get("skill_params") or None,
        )
        logger.info(f"Schedule {schedule['id']} firing for {agent_id}")
        await self._post(f"▶️ `{schedule['id']}` ran (agent `{agent_id}`).")
        asyncio.create_task(
            mgr.handle_message(agent_id, msg), name=f"schedule-{schedule['id']}")

    async def run(self) -> None:
        logger.info("Scheduler started"
                    + (f" (board → {self._board_channel()})" if self._board_channel() else ""))
        while True:
            try:
                if self._board_channel():
                    await self._reconcile_board()
                for schedule in sched.due_schedules(time.time()):
                    await self._fire(schedule)
            except Exception as e:
                logger.error(f"Scheduler tick failed: {e}", exc_info=True)
            await asyncio.sleep(self.tick)
