"""Tool-direct actions — schedules/triggers execute ONE tool with fixed args, zero LLM.

A schedule or trigger record may carry an `action` instead of (or besides) a
free-text instruction:

    action: {tool: shelly_switch, args: {device: office_light, state: off},
             narrate: false, silent: false}

When it fires, the bound tool runs deterministically through the engine's
dispatch path — so HITL gates, rate limits, access control, and audit logging
all still apply — with no model in the loop: sub-second, zero tokens, works
even when the local runtime is down, and (for webhooks) the event payload never
reaches a prompt. Optional `narrate: true` adds one local-model completion to
phrase the result for the channel; `silent: true` posts nothing.
"""

import logging

from src.core.base import IncomingMessage, Message, MessageRole

logger = logging.getLogger(__name__)

_NARRATE_PROMPT = (
    "You phrase automation results for a chat channel. Reply with ONE short, "
    "friendly sentence describing the outcome. No preamble, no markdown headers."
)


def parse_binding(skill: str, skill_params: str, tool: str, tool_args: str,
                  narrate: bool = False) -> tuple[str, dict, dict | None]:
    """Parse the optional skill/tool binding params of schedule_task /
    create_trigger (flat tool-call strings) into record fields.

    Returns (skill, skill_params_dict, action_or_None). Raises ValueError.
    """
    import json as _json
    if skill and tool:
        raise ValueError("give either skill= or tool=, not both")
    params: dict = {}
    if skill:
        from src.core.skills import get_skill
        if not get_skill(skill):
            raise ValueError(f"unknown skill '{skill}'")
        if skill_params:
            try:
                params = _json.loads(skill_params)
            except _json.JSONDecodeError as e:
                raise ValueError(f"skill_params must be JSON: {e}") from e
            if not isinstance(params, dict):
                raise ValueError("skill_params must be a JSON object")
    action = None
    if tool:
        args: dict = {}
        if tool_args:
            try:
                args = _json.loads(tool_args)
            except _json.JSONDecodeError as e:
                raise ValueError(f"tool_args must be JSON: {e}") from e
            if not isinstance(args, dict):
                raise ValueError("tool_args must be a JSON object")
        action = {"tool": tool, "args": args}
        if narrate:
            action["narrate"] = True
        err = validate_action(action)
        if err:
            raise ValueError(err)
    return skill, params, action


def validate_action(action: dict) -> str | None:
    """Validate an action dict at creation time. Returns an error string or None."""
    from src.core.tools import get_tool
    if not isinstance(action, dict) or not action.get("tool"):
        return "action must be a mapping with a 'tool' key"
    if not get_tool(action["tool"]):
        return f"unknown tool '{action['tool']}'"
    if not isinstance(action.get("args", {}), dict):
        return "action 'args' must be a mapping"
    return None


async def run_tool_action(mgr, record: dict, source: str) -> None:
    """Execute a record's bound tool and post the result. Never raises."""
    action = record.get("action") or {}
    agent_id = record.get("agent_id", "")
    rec_id = record.get("id", "?")
    try:
        connector = mgr.connectors.get(record.get("connector", "discord"))
        routing = mgr.agent_configs.get(agent_id, {}).get("routing", {})
        account = (routing.get(record.get("connector", "discord"), {}) or {}).get("account")
        msg = IncomingMessage(
            connector=record.get("connector", "discord"),
            channel_id=record.get("channel_id", ""),
            user_id=record.get("created_by") or "",
            user_name="automation",
            content=f"[action {source}:{rec_id}] {action.get('tool')}",
            bot_account=account,
        )
        # One synthetic tool call through the real dispatch path: HITL,
        # rate limits, agent allowlist, and audit all apply as usual.
        results = await mgr._dispatch_tools(
            agent_id, f"{source}:{rec_id}",
            [{"name": action["tool"], "arguments": action.get("args", {})}],
            connector, msg,
        )
        result_text = results[0]["content"] if results else "(no output)"
        logger.info(f"action {source}:{rec_id} → {action['tool']}: {result_text[:120]}")
        if getattr(mgr, "_bump", None):
            mgr._bump("action.direct")

        if action.get("silent") or not connector:
            return
        out = f"⚙️ `{action['tool']}` → {result_text}"
        if action.get("narrate"):
            out = await _narrate(mgr, action["tool"], result_text) or out
        await connector.send(record.get("channel_id", ""), out, bot_account=account)
    except Exception as e:
        logger.error(f"action {source}:{rec_id} failed: {e}", exc_info=True)


async def _narrate(mgr, tool_name: str, result_text: str) -> str | None:
    """One local-model completion phrasing the result (None → caller posts raw)."""
    local = mgr.llm_providers.get("local")
    if local is None:
        return None
    try:
        resp = await local.complete([
            Message(role=MessageRole.SYSTEM, content=_NARRATE_PROMPT),
            Message(role=MessageRole.USER,
                    content=f"Automation '{tool_name}' ran. Result: {result_text}"),
        ])
        if resp.stop_reason == "error" or not (resp.content or "").strip():
            return None
        return resp.content.strip()
    except Exception as e:
        logger.debug(f"narration failed, posting raw result: {e}")
        return None
