"""Tool scope registry — agent-created tools are private until promoted.

A tool created via create_tool belongs to its creator: only that agent can
see and call it. When another agent needs it, the owner (or the coordinator /
a privileged agent) promotes it to global via promote_tool.

Scope lives in a small JSON sidecar in the overlay tools dir:
    <overlay>/tools/.tool-scope.json
    { "<tool_name>": {"owner": "<agent_id>", "global": false}, ... }

Tools with no entry (core/module/hand-written tools) are global by nature.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SCOPE_FILENAME = ".tool-scope.json"


def _scope_path() -> Path | None:
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    if not overlay:
        return None
    return Path(overlay) / "tools" / SCOPE_FILENAME


def load_scope() -> dict[str, dict]:
    path = _scope_path()
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text()) or {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read tool scope registry: {e}")
        return {}


def _save_scope(scope: dict[str, dict]) -> None:
    path = _scope_path()
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scope, indent=2) + "\n")


def record_tool(name: str, owner: str) -> None:
    """Register an agent-created tool as private to its creator."""
    scope = load_scope()
    scope[name] = {"owner": owner, "global": False}
    _save_scope(scope)


def get_entry(name: str) -> dict | None:
    return load_scope().get(name)


def promote_to_global(name: str) -> bool:
    """Mark an agent-created tool as global. Returns False if unknown."""
    scope = load_scope()
    if name not in scope:
        return False
    scope[name]["global"] = True
    _save_scope(scope)
    return True


def hidden_tools_for_agent(agent_id: str) -> list[str]:
    """Tool names this agent must NOT see: private tools owned by others."""
    return [
        name
        for name, entry in load_scope().items()
        if not entry.get("global") and entry.get("owner") != agent_id
    ]
