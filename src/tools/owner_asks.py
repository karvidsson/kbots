"""Record an owner decision request through the engine, including from CLI tools."""

import json
import os

import aiohttp

from src.core.base import ToolContext
from src.core.tools import tool


@tool(
    name="ask_owner",
    category="communication",
    description=(
        "Ask the owner for a decision on one durable card. A nonempty default must say what happens if "
        "no answer arrives. Optional options become buttons; otherwise yes/no reactions. Returns immediately; "
        "the answer or expiry returns to this agent later. This never replaces a required tool approval."
    ),
)
async def ask_owner(
    ctx: ToolContext, question: str, default: str, options: list[str] = None, context: str = "", request_key: str = ""
) -> str:
    """Use a stable request_key for retries of one decision. Never treat silence as consent."""
    args = dict(
        agent_id=ctx.agent_id,
        question=question,
        default=default,
        options=options,
        context=context,
        request_key=request_key,
    )
    service = getattr(ctx.agent_manager, "_owner_asks", None)
    if service:
        try:
            return json.dumps(await service.ask(**args), ensure_ascii=False)
        except ValueError as exc:
            return str(exc)
    api, token = os.environ.get("KBOTS_INTERNAL_API"), os.environ.get("KBOTS_INTERNAL_TOKEN")
    if not api or not token:
        return "Waiting list unavailable: no engine connection."
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            async with session.post(
                api + "/owner-ask", json=args, headers={"Authorization": "Bearer " + token}
            ) as response:
                body = await response.json()
                return json.dumps(body, ensure_ascii=False)
    except Exception:
        return (
            "Ask delivery is unconfirmed. Retry with the same request_key and unchanged question/options/default "
            "to recover the existing request. Do not post another approval message."
        )
