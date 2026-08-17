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
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

SENSITIVE_KEYS = {"password", "secret", "token", "key", "api_key", "passphrase", "credential"}

# Value-shape patterns for secrets that appear INSIDE otherwise-innocent fields
# (e.g. a token embedded in a `command` string or a URL). Key-name redaction
# alone misses these — a `Bearer ghp_…` in an argument named `command` would
# pass straight through. Kept deliberately specific to limit false positives.
_SECRET_VALUE_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}"),                 # ghp_/gho_/ghs_/ghu_/ghr_
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),                        # OpenAI-style
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),               # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                         # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),                         # AWS temp key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                    # Google API key
    re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"),                   # Google OAuth token
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),  # JWT
    re.compile(r"\b[MNO][A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"),  # Discord bot token
    re.compile(r"(?i)\b(bearer|token|authorization)\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"(?i)X-Webhook-Secret['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{12,}"),
]


def scrub_value(s: str) -> str:
    """Mask any secret-shaped substrings inside a string. Idempotent-ish."""
    if not isinstance(s, str) or len(s) < 8:
        return s
    for pat in _SECRET_VALUE_PATTERNS:
        s = pat.sub("[REDACTED]", s)
    return s


def redact_secrets(obj):
    """Recursively redact secrets from any JSON-ish value.

    Combines key-name redaction (an argument literally named `token`) with
    value-shape scrubbing (a token embedded in a `command`/`url`/`body` string).
    Use this for anything persisted or shown to humans — audit log, tool log,
    training data, HITL approval messages.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and any(s in k.lower() for s in SENSITIVE_KEYS):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_secrets(v) for v in obj]
    if isinstance(obj, str):
        return scrub_value(obj)
    return obj


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
            try:
                self._path.chmod(0o600)  # audit log holds redacted-but-sensitive trace
            except OSError:
                pass

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
    """Redact sensitive values from a dict (key-name + value-shape scrubbing).

    Thin wrapper kept for existing callers; delegates to redact_secrets so
    embedded secrets (a token inside a `command` string) are caught too.
    """
    if not isinstance(data, dict):
        return data
    return redact_secrets(data)
