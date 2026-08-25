"""Session-scoped consent — 'ask the user once, then allow for the task'.

For sensitive capabilities (e.g. driving the user's real logged-in browser) that
shouldn't prompt on every call but must not be silent either. A capability is
granted per agent with a TTL that represents a task/session window; the tool
refuses until a grant is recorded. JSON-in-overlay so the MCP-tool process and
the engine share it (same pattern as runtime_state / triggers).
"""

import json
import logging
import time
from pathlib import Path

from src.core.base import overlay_state_path, overlay_state_read_path

logger = logging.getLogger(__name__)

_FILENAME = "session_consent.json"
DEFAULT_TTL = 8 * 3600  # seconds — roughly one working session/task


def _path() -> Path | None:
    """Written under the overlay's data/ dir, not the read-only overlay root."""
    return overlay_state_path(_FILENAME)


def _read_path() -> Path | None:
    """Read the current file, falling back to the pre-migration root-level one."""
    return overlay_state_read_path(_FILENAME)


def _load() -> dict:
    path = _read_path()
    if not path:
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    path = _path()
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _key(agent_id: str, capability: str) -> str:
    return f"{agent_id}:{capability}"


def is_granted(agent_id: str, capability: str, now: float | None = None) -> bool:
    """True if the agent has an unexpired grant for this capability."""
    now = now if now is not None else time.time()
    return float(_load().get(_key(agent_id, capability), 0)) > now


def grant(agent_id: str, capability: str, ttl: float = DEFAULT_TTL,
          now: float | None = None) -> float:
    """Grant a capability to an agent for `ttl` seconds. Returns the expiry ts."""
    now = now if now is not None else time.time()
    data = _load()
    expiry = now + ttl
    data[_key(agent_id, capability)] = expiry
    _save(data)
    logger.info(f"session_consent: granted {capability} to {agent_id} (ttl {int(ttl)}s)")
    return expiry


def revoke(agent_id: str, capability: str) -> None:
    data = _load()
    if data.pop(_key(agent_id, capability), None) is not None:
        _save(data)
