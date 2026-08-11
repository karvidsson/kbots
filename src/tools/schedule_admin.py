"""Scheduling tools — let an agent schedule its own future/recurring tasks.

The agent picks the timing (a cron expression, a repeat interval, or a one-off)
and the instruction to run. When it's due, the engine re-invokes this agent in
the same channel with that instruction — full tools, memory, and a reply.
"""

import json
import logging
import time
from datetime import datetime

import yaml

from src.core import runtime_state
from src.core import schedules as sched
from src.core.base import ToolContext, resolve_config_file
from src.core.tools import tool
from src.tools.hitl_admin import _is_admin

logger = logging.getLogger(__name__)


def _config_schedules_channel() -> str:
    """The board channel from config.yaml — the fallback when no runtime override."""
    cfg_file = resolve_config_file("config.yaml")
    if not cfg_file.exists():
        return ""
    try:
        cfg = yaml.safe_load(cfg_file.read_text()) or {}
    except yaml.YAMLError:
        return ""
    return str((cfg.get("schedules", {}) or {}).get("channel", "") or "")


@tool(
    name="schedule_task",
    description=(
        "Schedule a task for yourself to run later or on a repeat, in THIS "
        "channel. Give the instruction plus EXACTLY ONE timing (all times are "
        "SERVER LOCAL time):\n"
        "  • cron='M H D Mo W' — recurring cron (e.g. daily 8am → '0 8 * * *', "
        "weekdays 9am → '0 9 * * 1-5', top of every hour → '0 * * * *')\n"
        "  • every_minutes=N — repeat every N minutes\n"
        "  • in_minutes=N — once, N minutes from now\n"
        "  • once_at='YYYY-MM-DD HH:MM' — once, at that local time\n"
        "When it fires I'll carry out the instruction and reply here.\n"
        "For a WATCH ('when the report is out, analyze it'): use a recurring "
        "cron/every poll and bound it with until='YYYY-MM-DD' and/or max_runs=N "
        "so it auto-stops — e.g. poll weekday mornings for two weeks: "
        "cron='0 9 * * 1-5', until='2026-07-24'. Have the instruction cancel "
        "early once the event happens.\n"
        "Advanced bindings (instead of a free-text turn):\n"
        "  • skill='name' (+ skill_params='{...}' JSON) — fire a registered "
        "skill; its own llm/tool scoping applies (e.g. a local-model task).\n"
        "  • tool='name' + tool_args='{...}' JSON — run ONE tool with fixed "
        "args, deterministically, NO AI turn at all (fastest, cheapest; "
        "narrate=true adds a one-line local-model summary of the result)."
    ),
    category="admin",
)
async def schedule_task(
    ctx: ToolContext,
    instruction: str = "",
    cron: str = "",
    every_minutes: int = 0,
    in_minutes: int = 0,
    once_at: str = "",
    until: str = "",
    max_runs: int = 0,
    skill: str = "",
    skill_params: str = "",
    tool: str = "",
    tool_args: str = "",
    narrate: bool = False,
) -> str:
    if not ctx.channel_id:
        return "ERROR: no channel context — run this from a conversation channel."
    specs = [bool(cron), every_minutes > 0, in_minutes > 0, bool(once_at)]
    if sum(specs) != 1:
        return "ERROR: give exactly one of cron / every_minutes / in_minutes / once_at."
    try:
        from src.core.actions import parse_binding
        skill, skill_params_d, action = parse_binding(skill, skill_params, tool,
                                                      tool_args, narrate)
    except ValueError as e:
        return f"ERROR: {e}"
    if not instruction.strip() and not action:
        return "ERROR: give an instruction (or a tool= binding)."

    now = time.time()
    until_ts = 0.0
    if until.strip():
        try:
            fmt = "%Y-%m-%d %H:%M" if ":" in until else "%Y-%m-%d"
            until_ts = datetime.strptime(until.strip(), fmt).timestamp()
        except ValueError:
            return "ERROR: until must be 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'."
        if until_ts <= now:
            return "ERROR: until is in the past."

    try:
        if cron:
            spec_type, spec = "cron", cron.strip()
        elif every_minutes > 0:
            spec_type, spec = "every", str(int(every_minutes) * 60)
        elif in_minutes > 0:
            spec_type, spec = "once", str(now + int(in_minutes) * 60)
        else:
            dt = datetime.strptime(once_at.strip(), "%Y-%m-%d %H:%M")
            spec_type, spec = "once", str(dt.timestamp())
        rec = sched.create_schedule(
            ctx.agent_id, ctx.channel_id, instruction, ctx.user_id or "",
            spec_type=spec_type, spec=spec, now=now,
            until=until_ts, max_runs=max_runs,
            skill=skill, skill_params=skill_params_d, action=action,
        )
    except ValueError as e:
        return f"ERROR: {e}"

    if spec_type == "cron":
        when = f"on cron `{spec}` (server local time)"
    elif spec_type == "every":
        when = f"every {int(spec) // 60} min"
    else:
        when = "once at " + datetime.fromtimestamp(float(spec)).strftime("%Y-%m-%d %H:%M")
    bound = []
    if max_runs:
        bound.append(f"max {max_runs} run(s)")
    if until_ts:
        bound.append("until " + datetime.fromtimestamp(until_ts).strftime("%Y-%m-%d %H:%M"))
    bound_str = f" (auto-stops: {', '.join(bound)})" if bound else ""
    if action:
        what = (f"run `{action['tool']}({json.dumps(action.get('args', {}))})` "
                f"directly — no AI turn")
    elif skill:
        what = f"run skill `{skill}`"
    else:
        what = rec["instruction"]
    logger.info(f"schedule_task {rec['id']} ({spec_type}) for {ctx.agent_id}")
    return (f"⏰ Scheduled `{rec['id']}`: {when}{bound_str}, I'll → {what}\n"
            f"Cancel with cancel_schedule('{rec['id']}'); list with list_schedules.")


@tool(name="list_schedules", description="List this agent's scheduled tasks.", category="admin")
async def list_schedules(ctx: ToolContext) -> str:
    items = sched.list_schedules(ctx.agent_id)
    if not items:
        return "No scheduled tasks. Use schedule_task to add one."
    lines = ["Scheduled tasks:"]
    for s in items:
        st = s["spec_type"]
        if st == "cron":
            w = f"cron `{s['spec']}`"
        elif st == "every":
            w = f"every {int(s['spec']) // 60}min"
        else:
            w = "once @ " + datetime.fromtimestamp(float(s["spec"])).strftime("%Y-%m-%d %H:%M")
        state = "" if s.get("enabled") else " (done/disabled)"
        bound = []
        if s.get("max_runs"):
            bound.append(f"{s.get('run_count', 0)}/{s['max_runs']} runs")
        if s.get("until"):
            bound.append("until " + datetime.fromtimestamp(float(s["until"])).strftime("%Y-%m-%d"))
        bstr = f" [{', '.join(bound)}]" if bound else ""
        lines.append(f"  `{s['id']}` — {w}{state}{bstr}: {s['instruction']}")
    return "\n".join(lines)


@tool(name="cancel_schedule", description="Cancel a scheduled task by its id.", category="admin")
async def cancel_schedule(ctx: ToolContext, schedule_id: str) -> str:
    if sched.delete_schedule(schedule_id.strip()):
        return f"Cancelled schedule '{schedule_id}'."
    return f"No schedule with id '{schedule_id}'."


@tool(
    name="set_schedule_board",
    description=(
        "Set up (or move/clear) the schedule oversight channel — a Discord "
        "channel where every agent's scheduled tasks post as cards. New schedules "
        "post a ⏰ card, runs post ▶️, and finished ones post 🛑; an admin can react "
        "❌ on a ⏰ card to cancel it. Pass a channel_id to enable it there "
        "(create one first with discord_create_channel if needed), or an empty "
        "channel_id to clear the override (the board reverts to config — off "
        "unless schedules.channel is set). Applies live — no restart. Admin-only."
    ),
    category="admin",
)
async def set_schedule_board(ctx: ToolContext, channel_id: str, bot: str = "") -> str:
    """Point the board at channel_id; empty clears the override (reverts to config). Live."""
    if not _is_admin(ctx.user_id or ""):
        return ("ERROR: only an admin can configure the schedule board "
                "(it controls where schedules post and who can cancel them).")
    channel_id = (channel_id or "").strip()
    if not channel_id:
        # Clear the override rather than masking config with '': the board reverts
        # to the config value (unset = off unless schedules.channel is configured).
        runtime_state.clear_flag("schedules_channel")
        runtime_state.clear_flag("schedules_bot")
        logger.warning(f"Schedule board override cleared by admin {ctx.agent_id} (user {ctx.user_id})")
        cfg_ch = _config_schedules_channel()
        if cfg_ch:
            return (f"🗓 Cleared the runtime override — the board now follows config, which points "
                    f"at <#{cfg_ch}>. (To turn it off, unset schedules.channel in config.)")
        return ("🗓 Schedule oversight board **off** — cleared the runtime override and config has "
                "no schedules channel set. Scheduling itself still works.")
    runtime_state.set_flag("schedules_channel", channel_id)
    runtime_state.set_flag("schedules_bot", (bot or "").strip())
    logger.warning(f"Schedule board → {channel_id} by admin {ctx.agent_id} (user {ctx.user_id})")
    return (f"🗓 Schedule oversight board is now **live** in <#{channel_id}>. "
            f"New/finished schedules post there; react ❌ on a ⏰ card (as an admin) to cancel. "
            f"Applies immediately — no restart needed.")
