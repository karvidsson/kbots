"""Goal tools — multi-agent collaboration toward a shared, long-running goal.

A goal lives in its own auto-created Discord channel; every participant
(each with its own bot account) is routed there dynamically from the goal
store — no config edits, no restart. Tool result strings double as the
visible protocol messages: post them (or a tight summary) in the goal
channel so the team and the humans can follow along.

Pause protocol: any participant proposes (goal_propose), others support or
object with reasons (goal_vote), the owner closes the decision
(goal_decide) after the objection window. An adopted pause materializes
its wake condition as a schedule or webhook trigger that revives the goal.
When the team hits a wall only the user can clear, goal_block posts a
short "need to know / need from you" list and the goal waits.
"""

import json
import logging
import time
from datetime import datetime

import yaml

from src.core import goals as store
from src.core import schedules as sched
from src.core.base import ToolContext, resolve_config_file
from src.core.tools import tool

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "enabled": True,
    "create_tiers": ["coordinator", "privileged"],
    "category_id": "",
    "default_turn_budget": 30,
    "objection_window_hours": 24,
    "escalation_user": "",
    "alert_on_block": True,
}


def _cfg() -> dict:
    cfg_file = resolve_config_file("config.yaml")
    out = dict(_DEFAULTS)
    if cfg_file.exists():
        try:
            raw = yaml.safe_load(cfg_file.read_text()) or {}
            out.update(raw.get("goals", {}) or {})
            out["_discord"] = raw.get("connectors", {}).get("discord", {}) or {}
            out["_alert_channel"] = str(
                (raw.get("security", {}) or {}).get("alert_channel", "") or "")
        except yaml.YAMLError:
            pass
    out.setdefault("_discord", {})
    out.setdefault("_alert_channel", "")
    return out


def _agent_tier(agent_id: str) -> str:
    try:
        from src.tools.team import _load_team
        for a in _load_team().get("agents", []):
            if a.get("id") == agent_id:
                return a.get("agent_tier", "assistant")
    except Exception:
        pass
    return "assistant"


def _escalation_mention(cfg: dict) -> str:
    uid = str(cfg.get("escalation_user", "") or "")
    if not uid:
        try:
            from src.tools.team import _load_team
            for h in _load_team().get("humans", []):
                if h.get("access") == "owner":
                    uid = str((h.get("contact") or {}).get("discord", "") or "")
                    if uid:
                        break
        except Exception:
            pass
    return f"<@{uid}>" if uid else "@here"


def _is_owner_or_coordinator(goal: dict, agent_id: str) -> bool:
    return (goal["owner_agent"] == agent_id
            or _agent_tier(agent_id) in ("coordinator", "privileged"))


async def _create_goal_channel(ctx: ToolContext, title: str, cfg: dict) -> tuple[str, str]:
    """Create the goal's Discord channel. Returns (channel_id, note)."""
    guild_id = str(cfg["_discord"].get("guild_id", "") or "")
    if not guild_id or not ctx.vault:
        return "", "no guild_id configured or no vault access"
    from src.tools.discord_tools import _discord_post
    payload: dict = {"name": f"goal-{store._slugify(title)}"[:95], "type": 0,
                     "topic": f"Goal workstream: {title}"[:1000]}
    if cfg.get("category_id"):
        payload["parent_id"] = str(cfg["category_id"])
    result = await _discord_post(ctx.vault, f"/guilds/{guild_id}/channels", payload)
    if not result or result.get("error"):
        detail = (result or {}).get("detail", "no Discord token")
        return "", f"channel creation failed: {detail}"
    return str(result["id"]), ""


async def _post_to_channel(ctx: ToolContext, channel_id: str, content: str) -> str:
    """Post a message via the Discord REST API. Returns the message id ('' on failure)."""
    if not ctx.vault or not channel_id:
        return ""
    from src.tools.discord_tools import _discord_post
    result = await _discord_post(
        ctx.vault, f"/channels/{channel_id}/messages", {"content": content[:1900]})
    if not result or result.get("error"):
        return ""
    return str(result.get("id", ""))


async def _update_card(ctx: ToolContext, goal: dict) -> None:
    """Best-effort edit of the goal's kickoff card so status stays visible."""
    if not goal.get("card_message_id") or not ctx.vault:
        return
    from src.tools.discord_tools import _discord_patch
    await _discord_patch(
        ctx.vault,
        f"/channels/{goal['channel_id']}/messages/{goal['card_message_id']}",
        {"content": _card_text(goal)})


def _card_text(goal: dict) -> str:
    parts = [f"🎯 **GOAL: {goal['title']}** (`{goal['id']}`) — **{goal['status']}**",
             goal["description"][:500]]
    if goal["strategy"]:
        parts.append(f"**Strategy:** {goal['strategy'][:300]}")
    participants = ", ".join(
        f"{p['agent_id']}{' (owner)' if p['role'] == 'owner' else ''}"
        for p in store.list_participants(goal["id"]))
    parts.append(f"**Team:** {participants}")
    if goal["status"] == "paused" and goal["pause_reason"]:
        parts.append(f"⏸ {goal['pause_reason'][:200]}")
    return "\n".join(p for p in parts if p)


def _materialize_wake(goal: dict, actor: str, wake_condition: str) -> tuple[str, str]:
    """Turn a wake-condition JSON into a schedule/trigger. Returns (wake_ref, note)."""
    if not wake_condition.strip():
        return "", "no wake condition — resume manually with goal_resume"
    try:
        cond = json.loads(wake_condition)
    except json.JSONDecodeError:
        return "", "wake condition was not valid JSON — resume manually"
    kind = str(cond.get("type", ""))
    now = time.time()
    owner = goal["owner_agent"]
    channel = goal["channel_id"]
    try:
        if kind == "time":
            dt = datetime.strptime(str(cond.get("at", "")).strip(), "%Y-%m-%d %H:%M")
            rec = sched.create_schedule(
                owner, channel,
                f"Goal {goal['id']} wake: {goal['pause_reason'][:200]}. Review "
                f"the goal state and call goal_resume('{goal['id']}') or extend "
                f"the pause.", actor, spec_type="once", spec=str(dt.timestamp()), now=now)
            return f"schedule:{rec['id']}", f"wake scheduled at {cond['at']}"
        if kind == "metric":
            every = max(30, int(cond.get("every_minutes", 60)) * 60)
            rec = sched.create_schedule(
                owner, channel,
                f"Goal {goal['id']} watch: {cond.get('check', '')[:300]}. If the "
                f"condition is met, call goal_resume('{goal['id']}') and cancel "
                f"this schedule; otherwise reply exactly NO_REPLY.",
                actor, spec_type="every", spec=str(every), now=now)
            return (f"schedule:{rec['id']}",
                    f"watching every {every // 60} min: {cond.get('check', '')[:80]}")
        if kind == "webhook":
            from src.core import triggers
            rec, secret = triggers.create_trigger(
                str(cond.get("event", f"goal-{goal['id']}-wake")), owner, channel,
                f"Goal {goal['id']} wake event received. Review the payload and "
                f"call goal_resume('{goal['id']}') if the wait is over.", actor)
            return (f"trigger:{rec['id']}",
                    f"webhook trigger `{rec['event']}` armed — secret (shown once): {secret}")
        if kind == "email":
            return "", (f"waiting for email ({json.dumps(cond)[:120]}) — the "
                        f"owner's email watch will wake this channel; the goal "
                        f"context reminds the owner to check and goal_resume")
    except ValueError as e:
        return "", f"could not arm wake ({e}) — resume manually"
    return "", f"unknown wake type '{kind}' — resume manually with goal_resume"


def _clear_wake(goal: dict) -> None:
    ref = goal.get("wake_ref", "")
    if ref.startswith("schedule:"):
        sched.delete_schedule(ref.split(":", 1)[1])
    elif ref.startswith("trigger:"):
        from src.core import triggers
        triggers.delete_trigger(ref.split(":", 1)[1])


@tool(
    name="goal_create",
    description=(
        "Create a team goal with its own Discord channel where the named agents "
        "collaborate on it. You become the owner (facilitator): you advance "
        "phases (brainstorm → strategy → executing), assign tasks, and close "
        "decisions. participants is a comma-separated list of agent ids. If "
        "your tier can't create goals it is parked as 'proposed' in the current "
        "channel, and gets its own channel when a coordinator or human advances "
        "it."
    ),
    category="goals",
)
async def goal_create(ctx: ToolContext, title: str, description: str,
                      participants: str = "", turn_budget: int = 0) -> str:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return "ERROR: goals are disabled in config."
    if not title.strip():
        return "ERROR: give the goal a title."
    tier = _agent_tier(ctx.agent_id)
    may_create = tier in (cfg.get("create_tiers") or [])

    channel_id, chan_note = "", ""
    if may_create:
        channel_id, chan_note = await _create_goal_channel(ctx, title, cfg)
    anchored_here = not channel_id
    if anchored_here:
        channel_id = ctx.channel_id or ""
    if not channel_id:
        return "ERROR: could not create a channel and no current channel to anchor to."

    goal = store.create_goal(
        title, description, ctx.agent_id, channel_id,
        ctx.user_id or ctx.agent_id,
        turn_budget=int(turn_budget) or int(cfg["default_turn_budget"]),
        anchored=anchored_here)
    for pid in [p.strip() for p in participants.split(",") if p.strip()]:
        if pid != ctx.agent_id:
            store.add_participant(goal["id"], pid)
    if may_create:
        goal = store.update_goal(goal["id"], ctx.agent_id, status="brainstorm")

    card_id = await _post_to_channel(ctx, channel_id, _card_text(goal))
    if card_id:
        goal = store.update_goal(goal["id"], ctx.agent_id, card_message_id=card_id)

    lines = [f"🎯 Goal `{goal['id']}` created: **{goal['title']}** — status "
             f"{goal['status']}, turn budget {goal['turn_budget']}."]
    if anchored_here:
        lines.append(f"Anchored to THIS channel ({chan_note})."
                     if chan_note else "Anchored to this channel.")
    else:
        lines.append(f"Channel: <#{channel_id}> — collaboration happens there.")
    if not may_create:
        lines.append(f"Your tier ({tier}) can only PROPOSE goals — a coordinator "
                     f"or human must advance it with goal_set status=brainstorm.")
    else:
        lines.append("Kick off brainstorming in the goal channel; when a "
                     "direction emerges, record it with goal_set strategy=... .")
    return "\n".join(lines)


@tool(
    name="goal_status",
    description=("Show one goal in full (pass goal_id) or list all goals "
                 "(active first)."),
    category="goals",
)
async def goal_status(ctx: ToolContext, goal_id: str = "") -> str:
    if goal_id.strip():
        goal = store.get_goal(goal_id.strip())
        if not goal:
            return f"ERROR: unknown goal '{goal_id}'."
        lines = [_card_text(goal)]
        tasks = store.list_tasks(goal["id"])
        if tasks:
            lines.append("**Tasks:**")
            lines += [f"  #{t['id']} [{t['status']}] {t['title']}"
                      + (f" → {t['assignee']}" if t["assignee"] else "")
                      for t in tasks]
        for dec in store.open_decisions(goal["id"]):
            votes = store.list_votes(dec["id"])
            vstr = "; ".join(f"{v['agent_id']}: {v['stance']} ({v['reason'][:60]})"
                             for v in votes) or "no votes yet"
            ends = datetime.fromtimestamp(dec["window_ends_at"]).strftime("%Y-%m-%d %H:%M")
            lines.append(f"**Decision #{dec['id']}** ({dec['kind']}, by "
                         f"{dec['proposed_by']}, window ends {ends}): "
                         f"{dec['reason'][:150]} — {vstr}")
        return "\n".join(lines)
    items = store.list_goals()
    if not items:
        return "No goals. Create one with goal_create."
    order = {s: i for i, s in enumerate(
        ("executing", "strategy", "brainstorm", "blocked_on_user", "paused",
         "proposed", "done", "abandoned"))}
    items.sort(key=lambda g: order.get(g["status"], 99))
    return "\n".join(
        f"`{g['id']}` [{g['status']}] {g['title']} — owner {g['owner_agent']}, "
        f"<#{g['channel_id']}>" for g in items)


@tool(
    name="goal_set",
    description=(
        "Owner-only setter for a goal. field is one of: status (proposed/"
        "brainstorm/strategy/executing/paused/blocked_on_user/done/abandoned — "
        "transitions are validated), strategy, title, description, turn_budget, "
        "owner_agent. Coordinator-tier agents and humans may also advance a "
        "'proposed' goal."
    ),
    category="goals",
)
async def goal_set(ctx: ToolContext, goal_id: str, field: str, value: str) -> str:
    cfg = _cfg()
    goal = store.get_goal(goal_id.strip())
    if not goal:
        return f"ERROR: unknown goal '{goal_id}'."
    field = field.strip()
    if field not in ("status", "strategy", "title", "description",
                     "turn_budget", "owner_agent"):
        return "ERROR: field must be one of: status, strategy, title, description, turn_budget, owner_agent."
    if not _is_owner_or_coordinator(goal, ctx.agent_id):
        return (f"ERROR: only the owner ({goal['owner_agent']}) or a "
                f"coordinator-tier agent can change this goal.")
    leaving_proposed = (field == "status" and goal["status"] == "proposed"
                        and value.strip() != "proposed")
    try:
        kwargs = {field: int(value) if field == "turn_budget" else value.strip()}
        goal = store.update_goal(goal["id"], ctx.agent_id, **kwargs)
    except ValueError as e:
        return f"ERROR: {e}"

    # The goal only becomes real work here, so this is where it earns a channel.
    # Creating it at proposal time was impossible for an assistant-tier
    # proposer, which is most of the fleet, and nothing else ever created one
    # afterwards: such a goal kept the proposer's home channel forever and the
    # participants were routed into a private channel instead of a shared one.
    chan_note = ""
    if leaving_proposed and goal.get("anchored"):
        goal, chan_note = await _acquire_channel(ctx, goal, cfg)

    await _update_card(ctx, goal)
    return (f"✅ `{goal['id']}` {field} → {kwargs[field]}. Status: "
            f"{goal['status']}." + (f" {chan_note}" if chan_note else ""))


async def _acquire_channel(ctx: ToolContext, goal: dict, cfg: dict) -> tuple[dict, str]:
    """Move an anchored goal into a channel of its own. Best effort.

    A failure here must not undo the advance: the status change is the thing
    the caller asked for, and a goal running in a borrowed channel is exactly
    what it was doing a second ago. So this reports and leaves it anchored.
    """
    channel_id, err = await _create_goal_channel(ctx, goal["title"], cfg)
    if not channel_id:
        return goal, f"(still in this channel: {err or 'channel creation failed'})"
    try:
        goal = store.attach_channel(goal["id"], ctx.agent_id, channel_id)
    except ValueError as e:
        return goal, f"(channel created but not attached: {e})"
    card_id = await _post_to_channel(ctx, channel_id, _card_text(goal))
    if card_id:
        goal = store.update_goal(goal["id"], ctx.agent_id, card_message_id=card_id)
    return goal, f"Workstream moved to <#{channel_id}>."


@tool(
    name="goal_join",
    description="Join a goal as a member — you'll hear and speak in its channel.",
    category="goals",
)
async def goal_join(ctx: ToolContext, goal_id: str) -> str:
    goal = store.get_goal(goal_id.strip())
    if not goal:
        return f"ERROR: unknown goal '{goal_id}'."
    store.add_participant(goal["id"], ctx.agent_id)
    await _update_card(ctx, goal)
    return (f"✅ Joined `{goal['id']}` — you are now routed into "
            f"<#{goal['channel_id']}> and will hear every message there.")


@tool(
    name="goal_task",
    description=(
        "Manage a goal's tasks. action: add (title, detail, optional assignee) "
        "| assign (task_id, assignee) | doing (task_id) | done (task_id) | "
        "drop (task_id) | list."
    ),
    category="goals",
)
async def goal_task(ctx: ToolContext, goal_id: str, action: str, task_id: int = 0,
                    title: str = "", detail: str = "", assignee: str = "") -> str:
    goal = store.get_goal(goal_id.strip())
    if not goal:
        return f"ERROR: unknown goal '{goal_id}'."
    action = action.strip().lower()
    try:
        if action == "add":
            if not title.strip():
                return "ERROR: a new task needs a title."
            t = store.add_task(goal["id"], title, detail, assignee, ctx.agent_id)
            return f"✅ Task #{t['id']} added: {t['title']}" + (
                f" → {assignee}" if assignee else "")
        if action == "list":
            tasks = store.list_tasks(goal["id"], statuses=("open", "doing", "done"))
            if not tasks:
                return "No tasks yet — add one with goal_task(..., 'add', title=...)."
            return "\n".join(
                f"#{t['id']} [{t['status']}] {t['title']}"
                + (f" → {t['assignee']}" if t["assignee"] else "") for t in tasks)
        if action == "assign":
            t = store.update_task(task_id, ctx.agent_id, assignee=assignee)
            return f"✅ Task #{t['id']} → {t['assignee'] or 'unassigned'}"
        if action in ("doing", "done"):
            t = store.update_task(task_id, ctx.agent_id, status=action)
            return f"✅ Task #{t['id']} is {t['status']}: {t['title']}"
        if action == "drop":
            t = store.update_task(task_id, ctx.agent_id, status="dropped")
            return f"✅ Task #{t['id']} dropped."
    except ValueError as e:
        return f"ERROR: {e}"
    return "ERROR: action must be add/assign/doing/done/drop/list."


@tool(
    name="goal_update",
    description=("Post a progress note on a goal — what moved, what you found. "
                 "This is the record of progress; use it instead of free chat."),
    category="goals",
)
async def goal_update(ctx: ToolContext, goal_id: str, text: str) -> str:
    goal = store.get_goal(goal_id.strip())
    if not goal:
        return f"ERROR: unknown goal '{goal_id}'."
    if not text.strip():
        return "ERROR: nothing to record."
    store.log_event(goal["id"], ctx.agent_id, "update", text.strip())
    return f"📈 Update recorded on `{goal['id']}`: {text.strip()[:200]}"


@tool(
    name="goal_propose",
    description=(
        "Propose a team decision on a goal. kind: pause (needs reason + "
        "wake_condition), abandon, or strategy_change. Opens an objection "
        "window during which other participants goal_vote support or object "
        "with reasons; when the window closes the owner is woken to call "
        "goal_decide. wake_condition (for pause) is JSON, one of:\n"
        '  {"type":"time","at":"YYYY-MM-DD HH:MM"}\n'
        '  {"type":"metric","check":"what to check","every_minutes":60}\n'
        '  {"type":"webhook","event":"event-name"}\n'
        '  {"type":"email","from":"...","subject":"..."}\n'
        "ANNOUNCE the proposal in the goal channel so others can vote."
    ),
    category="goals",
)
async def goal_propose(ctx: ToolContext, goal_id: str, kind: str, reason: str,
                       wake_condition: str = "") -> str:
    goal = store.get_goal(goal_id.strip())
    if not goal:
        return f"ERROR: unknown goal '{goal_id}'."
    if not reason.strip():
        return "ERROR: a proposal needs a reason."
    kind = kind.strip().lower()
    if kind == "pause" and wake_condition.strip():
        try:
            json.loads(wake_condition)
        except json.JSONDecodeError:
            return "ERROR: wake_condition must be valid JSON (see tool description)."
    cfg = _cfg()
    hours = float(cfg["objection_window_hours"])
    ends = time.time() + hours * 3600
    try:
        dec = store.create_decision(goal["id"], kind, ctx.agent_id, reason,
                                    wake_condition.strip(), ends)
    except ValueError as e:
        return f"ERROR: {e}"
    try:
        sched.create_schedule(
            goal["owner_agent"], goal["channel_id"],
            f"Objection window for decision #{dec['id']} on goal {goal['id']} "
            f"has closed. Review the votes (goal_status '{goal['id']}') and "
            f"close it with goal_decide({dec['id']}, 'adopted'|'rejected').",
            ctx.agent_id, spec_type="once", spec=str(ends), now=time.time())
    except (ValueError, RuntimeError) as e:
        logger.warning(f"goal_propose: could not schedule window close: {e}")
    ends_str = datetime.fromtimestamp(ends).strftime("%Y-%m-%d %H:%M")
    return (f"🗳 **Decision #{dec['id']} on `{goal['id']}`: {kind.upper()}** "
            f"proposed by {ctx.agent_id}.\n"
            f"Reason: {reason.strip()}\n"
            + (f"Wake condition: {wake_condition.strip()}\n" if wake_condition.strip() else "")
            + f"Objection window closes {ends_str}. Participants: vote with "
            f"goal_vote({dec['id']}, 'support'|'object', reason). The owner "
            f"({goal['owner_agent']}) closes it with goal_decide — early if "
            f"everyone has voted.")


@tool(
    name="goal_vote",
    description=("Vote on an open goal decision: support or object, with your "
                 "reason. You can change your vote by voting again."),
    category="goals",
)
async def goal_vote(ctx: ToolContext, decision_id: int, stance: str,
                    reason: str) -> str:
    try:
        dec = store.vote(int(decision_id), ctx.agent_id, stance.strip().lower(),
                         reason)
    except ValueError as e:
        return f"ERROR: {e}"
    votes = store.list_votes(int(decision_id))
    goal = store.get_goal(dec["goal_id"])
    participants = {p["agent_id"] for p in store.list_participants(dec["goal_id"])}
    voted = {v["agent_id"] for v in votes}
    all_in = participants <= (voted | {dec["proposed_by"]})
    tally = (f"{sum(1 for v in votes if v['stance'] == 'support')} support / "
             f"{sum(1 for v in votes if v['stance'] == 'object')} object")
    out = (f"🗳 Vote recorded on #{decision_id} ({dec['kind']}): "
           f"{stance.strip().lower()} — {reason.strip()[:150]}. Tally: {tally}.")
    if all_in and goal:
        out += (f"\nAll participants have voted — {goal['owner_agent']} can "
                f"close early with goal_decide({decision_id}, ...).")
    return out


@tool(
    name="goal_decide",
    description=(
        "Close an open decision (owner or coordinator-tier only): outcome "
        "adopted, rejected, or withdrawn. Adopting a pause pauses the goal and "
        "arms its wake condition; adopting an abandon abandons the goal; "
        "adopting a strategy_change is recorded — apply it with goal_set "
        "strategy=... . If you proposed it yourself and there are standing "
        "objections, escalate to your reports_to hub or the user instead of "
        "overruling them."
    ),
    category="goals",
)
async def goal_decide(ctx: ToolContext, decision_id: int, outcome: str,
                      note: str = "") -> str:
    dec = store.get_decision(int(decision_id))
    if not dec:
        return f"ERROR: unknown decision #{decision_id}."
    goal = store.get_goal(dec["goal_id"])
    if not goal:
        return f"ERROR: goal '{dec['goal_id']}' vanished."
    if not _is_owner_or_coordinator(goal, ctx.agent_id):
        return (f"ERROR: only the owner ({goal['owner_agent']}) or a "
                f"coordinator-tier agent can close decisions.")
    outcome = outcome.strip().lower()
    try:
        dec = store.decide(int(decision_id), ctx.agent_id, outcome)
    except ValueError as e:
        return f"ERROR: {e}"
    lines = [f"⚖️ Decision #{decision_id} ({dec['kind']}) on `{goal['id']}`: "
             f"**{outcome}** by {ctx.agent_id}."
             + (f" {note.strip()}" if note.strip() else "")]
    if outcome == "adopted" and dec["kind"] == "pause":
        try:
            goal = store.update_goal(goal["id"], ctx.agent_id, status="paused",
                                     pause_reason=dec["reason"],
                                     wake_condition=dec["wake_condition"])
        except ValueError as e:
            return f"ERROR: decision recorded but pause failed: {e}"
        ref, wake_note = _materialize_wake(goal, ctx.agent_id, dec["wake_condition"])
        if ref:
            goal = store.update_goal(goal["id"], ctx.agent_id, wake_ref=ref)
        lines.append(f"⏸ Goal paused: {dec['reason'][:200]}")
        lines.append(f"Wake: {wake_note}")
    elif outcome == "adopted" and dec["kind"] == "abandon":
        try:
            goal = store.update_goal(goal["id"], ctx.agent_id, status="abandoned")
            lines.append("🪦 Goal abandoned.")
        except ValueError as e:
            return f"ERROR: decision recorded but abandon failed: {e}"
    await _update_card(ctx, goal)
    return "\n".join(lines)


@tool(
    name="goal_block",
    description=(
        "Declare the goal blocked on the user: the team hit a wall only they "
        "can clear. Give TWO short newline-separated lists — need_to_know "
        "(context the user needs) and need_from_user (concrete asks). Keep "
        "both SHORT. Post the returned briefing in the goal channel VERBATIM; "
        "the goal waits until the user replies, then the owner calls "
        "goal_resume."
    ),
    category="goals",
)
async def goal_block(ctx: ToolContext, goal_id: str, need_to_know: str,
                     need_from_user: str) -> str:
    goal = store.get_goal(goal_id.strip())
    if not goal:
        return f"ERROR: unknown goal '{goal_id}'."
    know = [x.strip("-• ").strip() for x in need_to_know.splitlines() if x.strip()]
    do = [x.strip("-• ").strip() for x in need_from_user.splitlines() if x.strip()]
    if not do:
        return "ERROR: need_from_user must contain at least one concrete ask."
    brief = json.dumps({"know": know, "do": do})
    try:
        goal = store.update_goal(goal["id"], ctx.agent_id,
                                 status="blocked_on_user", blocked_brief=brief)
    except ValueError as e:
        return f"ERROR: {e}"
    cfg = _cfg()
    mention = _escalation_mention(cfg)
    briefing = "\n".join(
        [f"🧱 **GOAL BLOCKED: {goal['title']}** (`{goal['id']}`)",
         f"{mention} — we need you before this can continue.", ""]
        + (["**Need to know:**"] + [f"- {k}" for k in know] + [""] if know else [])
        + ["**Need from you:**"]
        + [f"{i}. {d}" for i, d in enumerate(do, 1)]
        + ["", f"Reply in this channel. {goal['owner_agent']} will resume the "
               f"goal once answered."])
    if cfg.get("alert_on_block") and cfg.get("_alert_channel"):
        await _post_to_channel(
            ctx, cfg["_alert_channel"],
            f"🧱 Goal `{goal['id']}` is blocked on the user — see <#{goal['channel_id']}>.")
    await _update_card(ctx, goal)
    return briefing


@tool(
    name="goal_resume",
    description=(
        "Resume a paused or blocked goal (owner or coordinator-tier). Clears "
        "any armed wake schedule/trigger. to: phase to resume into — "
        "executing (default), brainstorm, or strategy."
    ),
    category="goals",
)
async def goal_resume(ctx: ToolContext, goal_id: str, note: str = "",
                      to: str = "executing") -> str:
    goal = store.get_goal(goal_id.strip())
    if not goal:
        return f"ERROR: unknown goal '{goal_id}'."
    if not _is_owner_or_coordinator(goal, ctx.agent_id):
        return (f"ERROR: only the owner ({goal['owner_agent']}) or a "
                f"coordinator-tier agent can resume this goal.")
    to = to.strip().lower() or "executing"
    if to not in ("executing", "brainstorm", "strategy"):
        return "ERROR: to must be executing, brainstorm, or strategy."
    _clear_wake(goal)
    try:
        goal = store.update_goal(goal["id"], ctx.agent_id, status=to,
                                 pause_reason="", wake_condition="",
                                 wake_ref="", blocked_brief="")
    except ValueError as e:
        return f"ERROR: {e}"
    if note.strip():
        store.log_event(goal["id"], ctx.agent_id, "resume", note.strip())
    await _update_card(ctx, goal)
    return (f"▶️ Goal `{goal['id']}` resumed → {to}."
            + (f" {note.strip()}" if note.strip() else ""))
