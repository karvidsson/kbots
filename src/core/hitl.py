"""HITL (Human-In-The-Loop) gate system.

Sensitive tools require human approval before execution. Requests are
persisted to SQLite, posted to Discord with reaction buttons, and polled
until approved/denied/timeout.

SQLite persistence means pending requests survive restarts.
"""

import asyncio
import logging
import time
import uuid

import aiosqlite

logger = logging.getLogger(__name__)

HITL_SCHEMA = """
CREATE TABLE IF NOT EXISTS hitl_pending (
    hitl_id     TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    description TEXT NOT NULL,
    message_id  TEXT,
    channel_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    approver    TEXT,
    created_at  REAL NOT NULL,
    resolved_at REAL,
    CONSTRAINT valid_status CHECK (status IN ('pending', 'approved', 'denied', 'timeout'))
);
CREATE INDEX IF NOT EXISTS idx_hitl_status ON hitl_pending(status);

CREATE TABLE IF NOT EXISTS runtime_flags (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class HITLGate:
    """Human-in-the-loop approval system for sensitive tool calls."""

    def __init__(self, config: dict, db: aiosqlite.Connection, connector=None,
                 admin_users: list | None = None):
        self.config = config
        self.db = db
        self.connector = connector

        # Config
        self.channel_id = config.get("channel")
        # Empty approvers must not mean "anyone can approve" — fall back to the
        # configured admin users. If neither is set, nobody can approve and gated
        # calls fail closed (time out → denied).
        self.approvers = set(config.get("approvers") or []) or set(admin_users or [])
        self.timeout = config.get("timeout", 1800)  # 30 min
        self.fail_mode = config.get("fail_mode", "closed")  # closed = deny if unreachable
        self.poll_interval = config.get("poll_interval", 3)
        self.gated_tools = set(config.get("gated_tools", []))
        # Runtime toggle — the approval gate can be switched off and back on
        # live. Backed by an overlay file (src/core/runtime_state) so a tool
        # running in the separate MCP process (e.g. set_hitl, or /admin hitl)
        # can flip it and the engine picks it up on the next message.
        self._config_default = config.get("enabled", True)
        self.enabled = self._config_default

    async def load_enabled(self) -> None:
        """Refresh the enabled flag from shared runtime state (cheap file read).

        Called at boot and once per message, so cross-process toggles apply.
        """
        from src.core import runtime_state
        self.enabled = bool(runtime_state.get_flag("hitl_enabled", self._config_default))

    async def set_enabled(self, enabled: bool) -> None:
        """Flip the approval gate live and persist it for all processes."""
        from src.core import runtime_state
        self.enabled = bool(enabled)
        runtime_state.set_flag("hitl_enabled", self.enabled)

    def is_gated(self, tool_name: str) -> bool:
        """Check if a tool requires HITL approval."""
        if tool_name in self.gated_tools:
            return True
        # Wildcard matching: "cloudflare_*" matches "cloudflare_dns_update"
        for pattern in self.gated_tools:
            if pattern.endswith("*") and tool_name.startswith(pattern[:-1]):
                return True
        return False

    async def request_approval(
        self, agent_id: str, tool_name: str, args: dict, description: str
    ) -> dict:
        """Request approval for a gated tool. Blocks until resolved.

        Returns: {"status": "approved"|"denied"|"timeout", "approver": str|None}
        """
        hitl_id = str(uuid.uuid4())[:8]
        now = time.time()

        # No channel/connector to ask on → resolve immediately instead of polling
        # for the full timeout. fail_mode=closed denies (a gated tool never runs
        # unapproved); fail_mode=open allows.
        if not self.connector or not self.channel_id:
            if self.fail_mode == "open":
                logger.warning(f"HITL: no channel for '{tool_name}' — fail_mode=open, allowing")
                return {"status": "approved", "approver": None, "reason": "no_channel_open"}
            logger.warning(
                f"HITL: '{tool_name}' requires approval but no HITL channel is configured — "
                "denying (fail-closed). Set security.hitl.channel to enable approvals."
            )
            return {"status": "denied", "approver": None, "reason": "no_channel"}

        # Persist to SQLite
        await self.db.execute(
            "INSERT INTO hitl_pending (hitl_id, agent_id, tool_name, args_json, description, "
            "channel_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (hitl_id, agent_id, tool_name, _redact_args(args), description,
             self.channel_id, now)
        )
        await self.db.commit()

        # Post approval message to Discord
        if self.connector and self.channel_id:
            try:
                msg_text = (
                    f"**🔒 HITL Approval Required**\n"
                    f"Agent: `{agent_id}`\n"
                    f"Tool: `{tool_name}`\n"
                    f"{description}\n"
                    f"ID: `{hitl_id}`\n\n"
                    f"React ✅ to approve or ❌ to deny."
                )
                sent_msg = await self.connector.send(self.channel_id, msg_text)

                # Store message_id and add reactions
                if sent_msg and hasattr(sent_msg, 'id'):
                    message_id = str(sent_msg.id)
                    await self.db.execute(
                        "UPDATE hitl_pending SET message_id = ? WHERE hitl_id = ?",
                        (message_id, hitl_id)
                    )
                    await self.db.commit()
                    # Add reaction buttons
                    try:
                        await sent_msg.add_reaction("✅")
                        await sent_msg.add_reaction("❌")
                    except Exception as e:
                        logger.warning(f"Failed to add HITL reactions: {e}")
            except Exception as e:
                logger.error(f"Failed to post HITL request: {e}")
                if self.fail_mode == "closed":
                    await self._resolve(hitl_id, "denied", None)
                    return {"status": "denied", "approver": None, "reason": "channel unreachable"}

        # Poll for resolution
        deadline = now + self.timeout
        while time.time() < deadline:
            status = await self._check_status(hitl_id)
            if status["status"] != "pending":
                return status
            await asyncio.sleep(self.poll_interval)

        # Timeout
        await self._resolve(hitl_id, "timeout", None)
        if self.fail_mode == "open":
            logger.warning(f"HITL timeout for {hitl_id} — fail_mode=open, allowing")
            return {"status": "approved", "approver": None, "reason": "timeout_open"}
        return {"status": "timeout", "approver": None}

    async def approve(self, hitl_id: str, approver_id: str) -> bool:
        """Approve a pending request. Called when a reaction is detected.

        Empty approvers means nobody is authorized (fail closed) — see __init__,
        where it falls back to admin_users.
        """
        if approver_id not in self.approvers:
            return False
        await self._resolve(hitl_id, "approved", approver_id)
        return True

    async def deny(self, hitl_id: str, approver_id: str) -> bool:
        """Deny a pending request. Only approvers can deny."""
        if approver_id not in self.approvers:
            return False
        await self._resolve(hitl_id, "denied", approver_id)
        return True

    async def _resolve(self, hitl_id: str, status: str, approver: str | None) -> None:
        """Update a request's status."""
        await self.db.execute(
            "UPDATE hitl_pending SET status = ?, approver = ?, resolved_at = ? "
            "WHERE hitl_id = ? AND status = 'pending'",
            (status, approver, time.time(), hitl_id)
        )
        await self.db.commit()
        logger.info(f"HITL {hitl_id}: {status}" + (f" by {approver}" if approver else ""))

    async def _check_status(self, hitl_id: str) -> dict:
        """Check current status of a request."""
        async with self.db.execute(
            "SELECT status, approver FROM hitl_pending WHERE hitl_id = ?",
            (hitl_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return {"status": "denied", "approver": None, "reason": "not_found"}
        return {"status": row[0], "approver": row[1]}

    async def recover_pending(self) -> int:
        """On startup, resolve expired pending requests. Returns count resolved."""
        now = time.time()
        async with self.db.execute(
            "SELECT hitl_id, created_at FROM hitl_pending WHERE status = 'pending'"
        ) as cursor:
            rows = await cursor.fetchall()

        resolved = 0
        for hitl_id, created_at in rows:
            if created_at + self.timeout < now:
                await self._resolve(hitl_id, "timeout", None)
                resolved += 1
                logger.info(f"HITL {hitl_id}: expired on startup")

        return resolved

    async def init_schema(self) -> None:
        """Create HITL tables if they don't exist."""
        await self.db.executescript(HITL_SCHEMA)
        await self.db.commit()


def _redact_args(args: dict) -> str:
    """Redact sensitive values from tool arguments for logging."""
    import json
    redacted = {}
    sensitive_keys = {"password", "secret", "token", "key", "api_key", "passphrase"}
    for k, v in args.items():
        if any(s in k.lower() for s in sensitive_keys):
            redacted[k] = "[REDACTED]"
        else:
            redacted[k] = v
    return json.dumps(redacted)


def hitl_result_message(tool_name: str, result: dict) -> str:
    """Agent-facing text for a non-approved HITL outcome.

    The no-channel case names the missing setting explicitly: without that, the
    agent reports a bare "denied" and the operator never learns the denial came
    from missing config rather than a human saying no.
    """
    if result.get("reason") in ("no_channel", "no discord token or HITL channel"):
        return (
            f"HITL: Tool '{tool_name}' requires human approval, but no approval "
            "channel is configured, so it was denied without asking anyone "
            "(fail-closed) and did NOT run. Tell the user plainly: approvals need "
            "`security.hitl.channel` in config.yaml set to a Discord channel ID "
            "(approval cards are posted there for ✅/❌ reaction) — until then, "
            "every approval-gated tool call is blocked."
        )
    return (
        f"HITL: Tool '{tool_name}' was {result['status']}. The tool did "
        "NOT run — nothing is queued or in progress. Tell the user plainly "
        "that the call needs approval (or was denied / timed out); do not "
        "describe it as running or pending completion."
    )


def build_hitl_description(tool_name: str, args: dict) -> str:
    """Build a human-readable description for the approval message."""
    extractors = {
        "send_email": lambda a: f"Send email to {a.get('to', '?')}, subject: {a.get('subject', '?')}",
        "team_add": lambda a: f"Add team member: {a.get('name', '?')} ({a.get('role', '?')})",
        "team_remove": lambda a: f"Remove team member: {a.get('id', '?')}",
        "install_mcp": lambda a: f"Install MCP server: {a.get('name', '?')} at {a.get('url', '?')}",
        "create_agent": lambda a: (
            f"Create agent '{a.get('display_name', a.get('name', '?'))}' "
            f"({a.get('tier', 'assistant')}, model {a.get('model', 'sonnet')}, "
            f"bot account {a.get('bot_account') or a.get('name', '?')})"
        ),
        # Show the actual source — the human approval IS the code review
        "create_tool": lambda a: (
            f"Create Python tool '{a.get('name', '?')}': {a.get('description', '')}\n"
            f"```python\n{str(a.get('python_code', ''))[:1500]}\n```"
        ),
        "promote_tool": lambda a: (
            f"Promote tool '{a.get('name', '?')}' from private to GLOBAL "
            f"(all agents will be able to use it)"
        ),
    }

    extractor = extractors.get(tool_name)
    if extractor:
        try:
            return extractor(args)
        except Exception:
            pass

    # Fallback: truncated JSON
    import json
    return json.dumps(args)[:200]
