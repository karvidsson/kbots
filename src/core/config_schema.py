"""Config schema validator — catches missing required keys and typos.

Dependency-free schema validation for config.yaml. Runs during preflight
before any connectors / backends spin up. Reports:
  - ERRORS: missing required keys, wrong types, invalid enum values
  - WARNINGS: unknown keys (probable typos)

Schema format:
  key: (type, required, allowed_values_or_None, nested_schema_or_None)

Keep this schema lean — only validate what matters for startup. Fine-grained
per-tool config is validated by the tool itself.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Schema: top-level → nested. None means "any dict accepted, no further check".
# (python_type, required, allowed_values, nested_schema)
_SCHEMA: dict[str, tuple] = {
    "kbots": (dict, True, None, {
        "name": (str, False, None, None),
        "log_level": (str, False, ["debug", "info", "warning", "error", "critical"], None),
        "data_dir": (str, False, None, None),
        "session_retention_days": (int, False, None, None),
    }),
    "connectors": (dict, False, None, {
        "discord": (dict, False, None, None),
        "http": (dict, False, None, None),
        "webhook": (dict, False, None, None),
    }),
    # Schedule oversight: a dedicated channel where agents' scheduled tasks are
    # posted, cancellable with an ❌ reaction; see /schedules for the full board.
    "schedules": (dict, False, None, {
        "channel": (str, False, None, None),
        "bot": (str, False, None, None),
    }),
    "defaults": (dict, False, None, {
        "llm": (dict, False, None, None),
        "session": (dict, False, None, None),
        "memory": (dict, False, None, {
            "backend": (str, False, ["sqlite"], None),
            "semantic_search": (bool, False, None, None),
            "decay_enabled": (bool, False, None, None),
            "max_results": (int, False, None, None),
            "reflection": (dict, False, None, None),
            "graph": (dict, False, None, {
                "enabled": (bool, False, None, None),
                "path": (str, False, None, None),
                "extract": (bool, False, None, None),
                "extract_batch": (int, False, None, None),
            }),
        }),
    }),
    "security": (dict, False, None, {
        "hitl": (dict, False, None, {
            "connector": (str, False, None, None),
            "enabled": (bool, False, None, None),
            "channel": (str, False, None, None),
            "approvers": (list, False, None, None),
            "timeout": (int, False, None, None),
            "fail_mode": (str, False, ["open", "closed"], None),
            "poll_interval": (int, False, None, None),
            "gated_tools": (list, False, None, None),
        }),
        "rate_limits": (dict, False, None, {
            "mode": (str, False, ["log", "enforce"], None),
            "defaults": (dict, False, None, None),
        }),
        "compression": (dict, False, None, {
            "enabled": (bool, False, None, None),
            "level": (str, False, ["lite", "standard"], None),
        }),
        "access_control": (dict, False, None, None),
        # Alerts (Layer 3 security alerts via Discord)
        "alert_channel": (str, False, None, None),
        "alert_bot": (str, False, None, None),
        # Permission watch (runtime rights-failure detection + escalation)
        "permission_watch": (dict, False, None, {
            "enabled": (bool, False, None, None),
            "agent": (str, False, None, None),
            "connector": (str, False, None, None),
            "channel": (str, False, None, None),
            "interval": (int, False, None, None),
            "cooldown": (int, False, None, None),
        }),
        # Health audit config (expected open ports, etc.)
        "expected_ports": (str, False, None, None),
    }),
    "admin_users": (dict, False, None, None),
    "agents": (dict, False, None, None),
}


def _type_name(t) -> str:
    return getattr(t, "__name__", str(t))


def _validate_node(node: Any, schema: dict, path: str,
                   errors: list[str], warnings: list[str]) -> None:
    """Recursive per-node validator."""
    if not isinstance(node, dict):
        errors.append(f"{path}: expected dict, got {_type_name(type(node))}")
        return

    for key, (py_type, required, allowed, nested) in schema.items():
        full = f"{path}.{key}" if path else key
        if key not in node:
            if required:
                errors.append(f"{full}: missing required key")
            continue
        val = node[key]
        if val is None:
            continue
        if not isinstance(val, py_type):
            errors.append(f"{full}: expected {_type_name(py_type)}, got {_type_name(type(val))}")
            continue
        if allowed is not None and val not in allowed:
            errors.append(f"{full}: value {val!r} not in {allowed}")
            continue
        if nested is not None and isinstance(val, dict):
            _validate_node(val, nested, full, errors, warnings)

    for key in node:
        if key not in schema:
            warnings.append(f"{path + '.' if path else ''}{key}: unknown key (typo?)")


def validate_config(config: dict) -> tuple[list[str], list[str]]:
    """Validate a loaded config dict. Returns (errors, warnings).

    Errors indicate missing required keys or wrong types — startup should fail.
    Warnings indicate probable typos (unknown keys) — startup can continue.
    """
    errors: list[str] = []
    warnings: list[str] = []
    _validate_node(config, _SCHEMA, "", errors, warnings)
    return errors, warnings
