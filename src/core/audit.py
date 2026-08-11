"""Audit logging — append-only structured JSONL.

Logs all tool executions, HITL events, rate limit hits, content safety alerts,
and auth events. Secrets are redacted. File is line-buffered, never truncated
by the application. Rotate with standard logrotate if needed.

Uses threading.Lock (not asyncio.Lock) because this is called from both async
(core agent manager) and sync (MCP server subprocess) contexts, and file I/O
via built-in open() is blocking. threading.Lock is safe in both, whereas
asyncio.Lock would break sync callers. Writes are fast (line-buffered append)
so loop blocking is negligible in practice.
"""

import atexit
import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

SENSITIVE_KEYS = {"password", "secret", "token", "key", "api_key", "passphrase", "credential"}


class AuditLog:
    """Append-only JSONL audit logger."""

    def __init__(self, log_path: str | Path = "data/audit.jsonl"):
        self._path = Path(log_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._file = None
        atexit.register(self.close)

    def _ensure_open(self):
        if self._file is None or self._file.closed:
            self._file = open(self._path, "a", buffering=1)  # line-buffered

    def log_tool(self, agent_id: str, tool_name: str, args: dict,
                 result: str, success: bool, duration_ms: int,
                 hitl: dict | None = None, rate_limit: dict | None = None) -> None:
        """Log a tool execution."""
        entry = {
            "ts": _iso_now(),
            "event": "tool_call",
            "agent": agent_id,
            "tool": tool_name,
            "args": _redact(args),
            "result": "success" if success else "error",
            "result_preview": result[:200] if result else None,
            "duration_ms": duration_ms,
        }
        if hitl:
            entry["hitl"] = hitl
        if rate_limit:
            entry["rate_limit"] = rate_limit
        self._write(entry)

    def log_hitl(self, hitl_id: str, agent_id: str, tool_name: str,
                 status: str, approver: str | None = None,
                 wait_ms: int | None = None) -> None:
        """Log an HITL event."""
        self._write({
            "ts": _iso_now(),
            "event": "hitl",
            "hitl_id": hitl_id,
            "agent": agent_id,
            "tool": tool_name,
            "status": status,
            "approver": approver,
            "wait_ms": wait_ms,
        })

    def log_rate_limit(self, agent_id: str, tool_name: str,
                       count: int, limit: int, window: str) -> None:
        """Log a rate limit hit."""
        self._write({
            "ts": _iso_now(),
            "event": "rate_limit",
            "agent": agent_id,
            "tool": tool_name,
            "count": count,
            "limit": limit,
            "window": window,
        })

    def log_content_safety(self, agent_id: str, score: int,
                           source: str, patterns: list[str]) -> None:
        """Log a content safety alert."""
        self._write({
            "ts": _iso_now(),
            "event": "content_safety",
            "agent": agent_id,
            "score": score,
            "source": source,
            "patterns": patterns,
        })

    def log_auth(self, event_type: str, detail: str) -> None:
        """Log an authentication event."""
        self._write({
            "ts": _iso_now(),
            "event": "auth",
            "type": event_type,
            "detail": detail,
        })

    def log_message(self, agent_id: str, user_id: str, channel_id: str,
                    direction: str) -> None:
        """Log a message event (inbound/outbound). Content not logged."""
        self._write({
            "ts": _iso_now(),
            "event": "message",
            "agent": agent_id,
            "user": user_id,
            "channel": channel_id,
            "direction": direction,
        })

    def _write(self, entry: dict) -> None:
        """Write an entry to the log file."""
        try:
            with self._lock:
                self._ensure_open()
                self._file.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error(f"Audit log write failed: {e}")

    def close(self) -> None:
        """Close the log file."""
        if self._file and not self._file.closed:
            self._file.close()


def _iso_now() -> str:
    """Current time in ISO 8601 format."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _redact(data: dict) -> dict:
    """Redact sensitive values from a dict."""
    if not isinstance(data, dict):
        return data
    redacted = {}
    for k, v in data.items():
        if any(s in k.lower() for s in SENSITIVE_KEYS):
            redacted[k] = "[REDACTED]"
        elif isinstance(v, dict):
            redacted[k] = _redact(v)
        else:
            redacted[k] = v
    return redacted
