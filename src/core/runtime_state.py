"""Cross-process runtime flags — small mutable state both the engine and the
MCP-tool process need to share (e.g. the HITL killswitch).

Backed by a JSON file in the overlay (resolved via KBOTS_OVERLAY), so a tool
running in the separate MCP process can flip a flag and the main engine picks
it up. Tiny and low-churn — same pattern as triggers.json / tool-scope.json.
"""

import json
import logging
from pathlib import Path

from src.core.base import overlay_state_path, overlay_state_read_path

logger = logging.getLogger(__name__)

_FILENAME = "runtime.json"


def _path() -> Path | None:
    """Written under the overlay's data/ dir, not the read-only overlay root."""
    return overlay_state_path(_FILENAME)


def _read_path() -> Path | None:
    """Read the current file, falling back to the pre-migration root-level one."""
    return overlay_state_read_path(_FILENAME)


def get_flag(name: str, default=None):
    path = _read_path()
    if not path:
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
    source = _read_path()
    if source:
        try:
            data = json.loads(source.read_text())
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
    source = _read_path()
    path = _path()
    if not source or not path:
        return
    try:
        data = json.loads(source.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if name in data:
        del data[name]
        # Clearing a flag that only exists in the legacy file has to WRITE the
        # current one, not edit the legacy in place: otherwise the flag reads
        # cleared, the next set_flag migrates the legacy copy forward, and the
        # cleared flag comes back.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
