"""Event-trigger tools — let an agent set up 'when <event> fires, do <X>'.

The agent registers a trigger; the user then points their smart-home hub (or
any system) at the webhook URL this returns. When that event POSTs in, the
webhook connector re-invokes the agent with the stored instruction.
"""

import logging

import yaml

from src.core import triggers as trig
from src.core.base import ToolContext, resolve_config_file
from src.core.tools import tool

logger = logging.getLogger(__name__)


def _webhook_base() -> str:
    host, port = "127.0.0.1", 8090
    cfg_file = resolve_config_file("config.yaml")
    if cfg_file.exists():
        try:
            cfg = yaml.safe_load(cfg_file.read_text()) or {}
            wh = cfg.get("connectors", {}).get("webhook", {}) or {}
            host = wh.get("public_host") or wh.get("host") or host
            port = wh.get("port", port)
        except yaml.YAMLError:
            pass
    return f"http://{host}:{port}"


@tool(
    name="create_trigger",
    description=(
        "Set up an event trigger: when a named external event fires (via the "
        "webhook), this agent runs the given instruction in this channel. Use "
        "for 'when X happens, do Y' automations (e.g. a smart-home switch). "
        "Returns the webhook URL + payload for the user to configure in their "
        "device/hub. Requires human approval.\n"
        "Advanced bindings (instead of a free-text turn):\n"
        "  • skill='name' (+ skill_params='{...}' JSON) — fire a registered "
        "skill (its own llm/tool scoping applies).\n"
        "  • tool='name' + tool_args='{...}' JSON — run ONE tool with fixed "
        "args, NO AI turn at all; the webhook payload never reaches a prompt "
        "(best for fixed device automations; narrate=true adds a one-line "
        "local-model summary)."
    ),
    category="admin",
    hitl=True,
)
async def create_trigger(ctx: ToolContext, event: str, instruction: str = "",
                         skill: str = "", skill_params: str = "",
                         tool: str = "", tool_args: str = "",
                         narrate: bool = False) -> str:
    """Register an event → action trigger.

    event: short name for the event (e.g. 'kitchen_light_off'). instruction:
    what to do when it fires (e.g. 'turn on the hallway light and tell me').
    """
    if not ctx.channel_id:
        return "ERROR: no channel context — run this from a conversation channel."
    try:
        from src.core.actions import parse_binding
        skill, skill_params_d, action = parse_binding(skill, skill_params, tool,
                                                      tool_args, narrate)
        rec, secret = trig.create_trigger(
            event=event,
            agent_id=ctx.agent_id,
            channel_id=ctx.channel_id,
            instruction=instruction,
            created_by=ctx.user_id or "",
            skill=skill, skill_params=skill_params_d, action=action,
        )
    except ValueError as e:
        return f"ERROR: {e}"

    if action:
        what = f"run `{action['tool']}` with fixed args (no AI turn)"
    elif skill:
        what = f"run skill `{skill}`"
    else:
        what = rec["instruction"]
    url = f"{_webhook_base()}/event/{rec['id']}"
    logger.info(f"create_trigger: '{rec['event']}' → {ctx.agent_id} ({rec['id']})")
    return (
        f"Trigger '{rec['id']}' set: when **{rec['event']}** fires, I'll → "
        f"{what}\n\n"
        f"This registration has its OWN secret (shown once — copy it now):\n"
        f"```\ncurl -X POST {url} \\\n"
        f"  -H 'X-Webhook-Secret: {secret}' \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -d '{{}}'\n```\n"
        f"Give that URL + secret to the provider/device for this event only. "
        f"Any JSON body is passed to me as event data. "
        f"Remove it with delete_trigger('{rec['id']}'); pause ALL triggers with "
        f"`/admin triggers off`."
    )


@tool(
    name="list_triggers",
    description="List this agent's event triggers.",
    category="admin",
)
async def list_triggers(ctx: ToolContext) -> str:
    items = trig.list_triggers(ctx.agent_id)
    if not items:
        return "No triggers set. Use create_trigger to add one."
    lines = ["Triggers:"]
    for t in items:
        state = "" if t.get("enabled") else " (disabled)"
        lines.append(f"  `{t['id']}` — on **{t['event']}**{state}: {t['instruction']}")
    return "\n".join(lines)


@tool(
    name="delete_trigger",
    description="Delete an event trigger by its id.",
    category="admin",
    hitl=True,
)
async def delete_trigger(ctx: ToolContext, trigger_id: str) -> str:
    if trig.delete_trigger(trigger_id.strip()):
        return f"Trigger '{trigger_id}' deleted."
    return f"No trigger with id '{trigger_id}'."
