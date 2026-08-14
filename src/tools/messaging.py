"""Outbound messaging tools — reach people beyond Discord.

Slack and SMS (Twilio). Each is HITL-gated (like send_email) and
reads its credentials from the vault, degrading with clear guidance when a
token isn't configured. Tokens (add via the vault):
- Slack:    secrets/slack-bot-token          (xoxb-… bot token)
- Twilio:   secrets/twilio-account-sid,
            secrets/twilio-auth-token,
            secrets/twilio-from-number         (+15551234567)
"""

from src.core.base import ToolContext
from src.core.tools import tool


def _secret(ctx: ToolContext, key: str) -> str | None:
    if not ctx.vault:
        return None
    return ctx.vault.get(f"secrets/{key}") or ctx.vault.get(key.upper().replace("-", "_"))


@tool(name="slack_send", description="Send a message to a Slack channel or user",
      category="messaging", hitl=True)
async def slack_send(ctx: ToolContext, channel: str, text: str) -> str:
    """Post a message to Slack via chat.postMessage.

    Args:
        channel: channel ID (e.g. C0123ABC) or name (#general), or a user/DM ID.
        text: message text (Slack markdown supported).
    Requires a bot token in the vault as secrets/slack-bot-token.
    """
    token = _secret(ctx, "slack-bot-token")
    if not token:
        return "No Slack token configured. Add a bot token to the vault as secrets/slack-bot-token."
    if not channel or not text:
        return "Both 'channel' and 'text' are required."

    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json={"channel": channel, "text": text},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json()
    except aiohttp.ClientError as e:
        return f"Slack request failed: {e}"
    if not data.get("ok"):
        return f"Slack error: {data.get('error', 'unknown')}"
    return f"Message sent to Slack {channel} (ts {data.get('ts')})."


@tool(name="sms_send", description="Send an SMS via Twilio",
      category="messaging", hitl=True)
async def sms_send(ctx: ToolContext, to: str, text: str) -> str:
    """Send an SMS text message via Twilio.

    Args:
        to: destination phone number in E.164 form (e.g. +15551234567).
        text: message body.
    Requires in the vault: secrets/twilio-account-sid, secrets/twilio-auth-token,
    secrets/twilio-from-number.
    """
    sid = _secret(ctx, "twilio-account-sid")
    token = _secret(ctx, "twilio-auth-token")
    from_number = _secret(ctx, "twilio-from-number")
    missing = [k for k, v in (("twilio-account-sid", sid), ("twilio-auth-token", token),
                              ("twilio-from-number", from_number)) if not v]
    if missing:
        return "Twilio not configured. Add to the vault: " + ", ".join(f"secrets/{m}" for m in missing)
    if not to or not text:
        return "Both 'to' and 'text' are required."

    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                auth=aiohttp.BasicAuth(sid, token),
                data={"To": to, "From": from_number, "Body": text},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                status = r.status
                data = await r.json()
    except aiohttp.ClientError as e:
        return f"Twilio request failed: {e}"
    if status >= 400:
        return f"Twilio error: {data.get('message', 'unknown')} (code {data.get('code')})"
    return f"SMS sent to {to} (sid {data.get('sid')}, status {data.get('status')})."
