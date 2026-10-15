"""Ask the owner to put a secret into the vault, including from CLI tools."""

import json
import os

import aiohttp

from src.core.base import ToolContext
from src.core.tools import tool


@tool(
    name="ask_owner_secret",
    category="communication",
    description=(
        "Ask the owner to store a secret (a password, token or key) in the kbots vault. Posts one card with "
        "an owner-only button that opens a private form; the value goes straight to the vault and is never "
        "shown to you. key: the vault key your tool reads (must use an allowed prefix, e.g. kvm_). label: "
        "what it is, e.g. 'KVM password for lab'. purpose: why you need it. Returns immediately; you are "
        "told when it is saved. Never ask for secrets in chat."
    ),
)
async def ask_owner_secret(ctx: ToolContext, key: str, label: str, purpose: str, request_key: str = "") -> str:
    """Use a stable request_key for retries of one request."""
    args = dict(agent_id=ctx.agent_id, key=key, label=label, purpose=purpose, request_key=request_key)
    service = getattr(ctx.agent_manager, "_owner_secrets", None)
    if service:
        try:
            return json.dumps(await service.request(**args), ensure_ascii=False)
        except ValueError as exc:
            return str(exc)
    api, token = os.environ.get("KBOTS_INTERNAL_API"), os.environ.get("KBOTS_INTERNAL_TOKEN")
    if not api or not token:
        return "Secret requests unavailable: no engine connection."
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            async with session.post(
                api + "/owner-secret", json=args, headers={"Authorization": "Bearer " + token}
            ) as response:
                return json.dumps(await response.json(), ensure_ascii=False)
    except Exception:
        return ("Secret request delivery is unconfirmed. Retry with the same request_key to recover the "
                "existing request. Do not post another one or ask in chat.")
