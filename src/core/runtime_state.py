"""Cross-process runtime flags — small mutable state both the engine and the
MCP-tool process need to share (e.g. the HITL killswitch).

Backed by a JSON file in the overlay (resolved via KBOTS_OVERLAY), so a tool
running in the separate MCP process can flip a flag and the main engine picks
it up. Tiny and low-churn — same pattern as triggers.json / tool-scope.json.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_FILENAME = "runtime.json"


def _path() -> Path | None:
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    return Path(overlay) / _FILENAME if overlay else None


def get_flag(name: str, default=None):
    path = _path()
    if not path or not path.exists():
        return default
    try:
        return json.loads(path.read_text()).get(name, default)
    except (json.JSONDecodeError, OSError):
        return default


def set_flag(name: str, value) -> None:
    path = _path()
    if not path:
        raise RuntimeError("KBOTS_OVERLAY not set — cannot persist runtime state")
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    data[name] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    try:
        path.chmod(0o600)  # holds hitl_enabled — don't let other users flip the gate
    except OSError:
        pass


def clear_flag(name: str) -> None:
    """Remove a flag entirely so its config default governs again (a set_flag
    to '' would instead *mask* the config value — clear_flag reverts to config)."""
    path = _path()
    if not path or not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if name in data:
        del data[name]
        path.write_text(json.dumps(data, indent=2) + "\n")
