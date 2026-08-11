"""Event triggers — 'when <event> fires, have <agent> do <instruction>'.

Stored as JSON in the overlay so both the main process (webhook connector,
which fires them) and the MCP server process (tools, which create them) share
one source of truth without a DB connection.

Security model:
  - Each trigger has its OWN secret, generated at creation and shown once.
    Only its SHA-256 hash is stored, so triggers.json holds no usable secret.
    A leak of one provider's secret can't fire any other trigger.
  - A global killswitch (`enabled`) instantly disables ALL event-triggered
    inference without deleting anything.

Store format: {"enabled": bool, "triggers": [...]}. A bare list from an older
version is migrated on read.
"""

import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets as _secrets
from pathlib import Path

logger = logging.getLogger(__name__)

_FILENAME = "triggers.json"
_EVENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _path() -> Path | None:
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    return Path(overlay) / _FILENAME if overlay else None


def _load_doc() -> dict:
    path = _path()
    if not path or not path.exists():
        return {"enabled": True, "triggers": []}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read triggers: {e}")
        return {"enabled": True, "triggers": []}
    if isinstance(data, list):  # migrate old bare-list format
        return {"enabled": True, "triggers": data}
    if isinstance(data, dict):
        data.setdefault("enabled", True)
        data.setdefault("triggers", [])
        return data
    return {"enabled": True, "triggers": []}


def _save_doc(doc: dict) -> None:
    path = _path()
    if not path:
        raise RuntimeError("KBOTS_OVERLAY not set — cannot persist triggers")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    with contextlib.suppress(OSError):
        path.chmod(0o600)  # secret hashes live here — keep it owner-only


# --- killswitch ---

def is_enabled() -> bool:
    """False disables ALL event-triggered inference (the global killswitch)."""
    return bool(_load_doc().get("enabled", True))


def set_enabled(enabled: bool) -> None:
    doc = _load_doc()
    doc["enabled"] = bool(enabled)
    _save_doc(doc)


# --- helpers ---

def normalize_event(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "_")


def is_valid_event(name: str) -> bool:
    return bool(_EVENT_RE.match(name))


def _hash(secret: str) -> str:
    return hashlib.sha256((secret or "").encode()).hexdigest()


def verify_secret(trigger: dict, provided: str) -> bool:
    """Constant-time check of a provided secret against a trigger's stored hash."""
    return hmac.compare_digest(trigger.get("secret_hash", ""), _hash(provided))


def _next_id(triggers: list[dict]) -> str:
    nums = [int(t["id"][1:]) for t in triggers
            if t.get("id", "").startswith("t") and t["id"][1:].isdigit()]
    return f"t{(max(nums) + 1) if nums else 1}"


# --- CRUD ---

def create_trigger(
    event: str,
    agent_id: str,
    channel_id: str,
    instruction: str,
    created_by: str,
    connector: str = "discord",
    skill: str = "",
    skill_params: dict | None = None,
    action: dict | None = None,
) -> tuple[dict, str]:
    """Register a trigger. Returns (record, plaintext_secret).

    The secret is returned ONCE (only its hash is stored). Raises ValueError.
    skill: fire a registered skill when the trigger hits (its llm pin / tool
    scoping apply). action: tool-direct binding {tool, args, narrate?, silent?}
    — executes ONE tool with fixed args and NO LLM turn at all (the webhook
    payload never reaches a prompt).
    """
    event = normalize_event(event)
    if not is_valid_event(event):
        raise ValueError(
            f"Invalid event name {event!r} — use lowercase letters, digits, "
            f"'_', '.', '-' (e.g. 'kitchen_light_off')"
        )
    if action is not None:
        from src.core.actions import validate_action
        err = validate_action(action)
        if err:
            raise ValueError(f"Invalid action: {err}")
    if not instruction.strip() and not action:
        raise ValueError("Instruction is required")
    doc = _load_doc()
    secret = _secrets.token_urlsafe(24)
    record = {
        "id": _next_id(doc["triggers"]),
        "event": event,
        "agent_id": agent_id,
        "channel_id": channel_id,
        "connector": connector,
        "instruction": instruction.strip(),
        "created_by": created_by,
        "enabled": True,
        "secret_hash": _hash(secret),
        "skill": skill or "",
        "skill_params": skill_params or {},
        "action": action or None,
    }
    doc["triggers"].append(record)
    _save_doc(doc)
    return record, secret


def list_triggers(agent_id: str | None = None) -> list[dict]:
    triggers = _load_doc()["triggers"]
    return [t for t in triggers if not agent_id or t.get("agent_id") == agent_id]


def get_by_id(trigger_id: str) -> dict | None:
    return next((t for t in _load_doc()["triggers"] if t.get("id") == trigger_id), None)


def delete_trigger(trigger_id: str) -> bool:
    doc = _load_doc()
    remaining = [t for t in doc["triggers"] if t.get("id") != trigger_id]
    if len(remaining) == len(doc["triggers"]):
        return False
    doc["triggers"] = remaining
    _save_doc(doc)
    return True
