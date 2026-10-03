"""Presentation and pending metadata only. Nothing here grants tool approval."""

import json
import re

from src.core.audit import redact_secrets

# MCP owns its reaction poll. These rows let the engine see it waiting, without
# introducing a second decision store or persisting email arguments again.
MCP_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS hitl_mcp_pending (
    hitl_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    approvers_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""


def clip(text: str, limit: int) -> str:
    """Bound Discord UTF-16 units, including the truncation marker."""
    raw = text.encode("utf-16-le", errors="replace")
    if len(raw) <= limit * 2:
        return text
    return raw[: (limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def literal(value, limit: int, *, multiline=False) -> str:
    text = str(value).replace("`", "'")
    text = re.sub(r"@(?:everyone|here)\b|<@!?[0-9]+>|<@&[0-9]+>", lambda m: m[0].replace("@", "＠"), text)
    if not multiline:
        text = " ".join(text.split())
    # Do not let control characters or bidi overrides disguise the preview.
    text = "".join(c for c in text if c.isprintable() or (multiline and c == "\n"))
    return clip(text if multiline else " ".join(text.split()), limit)


def email_approval_card(agent_id: str, hitl_id: str, args: dict) -> str:
    """A bounded plain-text preview on the one existing approval card.

    Sanitise first, then redact before truncating so display cleanup or a cut
    through a credential cannot expose it.
    Attachment paths and contents are deliberately neither read nor displayed.
    """
    safe = redact_secrets({key: args.get(key, "") for key in ("to", "subject", "body")}, conservative=True)
    lines = str(safe["body"]).splitlines()
    body = "\n".join(lines[:11]) + "\n…" if len(lines) > 12 else "\n".join(lines)
    body = literal(body, 1000, multiline=True) or "(empty body)"
    attachments = "present; contents not shown" if args.get("attachments") else "none"
    return (
        "**HITL Approval Required**\n"
        f"Agent: `{literal(agent_id, 60)}`\nTool: `send_email`\n"
        f"ID: `{literal(hitl_id, 40)}`\n"
        "```\n"
        f"To: {literal(safe['to'], 180)}\n"
        f"Subject: {literal(safe['subject'], 180)}\n"
        f"Attachments: {attachments}\n"
        f"Body preview (redacted, shortened if needed):\n{body}\n"
        "```\nReact ✅ to approve or ❌ to deny. This approves the original email.\n"
        "To change it, deny and ask the agent for a revised email."
    )


async def pending_for(gate, recipient: str, now: float) -> list[dict]:
    """Read both existing gate paths; never resolve or extend a request."""
    if recipient not in {str(x) for x in gate.approvers}:
        return []
    async with gate.db.execute(
        "SELECT agent_id,tool_name,channel_id,message_id,created_at FROM hitl_pending "
        "WHERE status='pending' AND created_at + ? > ?",
        (gate.timeout, now),
    ) as cursor:
        engine = await cursor.fetchall()
    async with gate.db.execute(
        "SELECT agent_id,tool_name,channel_id,message_id,created_at,approvers_json "
        "FROM hitl_mcp_pending WHERE expires_at > ?",
        (now,),
    ) as cursor:
        mcp = await cursor.fetchall()
    keys = ("agent_id", "tool_name", "channel_id", "message_id", "created_at")
    rows = [dict(zip(keys, row)) for row in engine]
    for row in mcp:
        if recipient in json.loads(row[5]):
            rows.append(dict(zip(keys, row[:5])))
    return sorted(rows, key=lambda row: row["created_at"])
