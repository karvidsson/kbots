"""Reply → lessons map, so a 👍/👎 reaction can score the lessons a reply used.

When an agent's reply drew on recalled lesson memories, we record
{reply_message_id: {agent_id, lessons:[ids]}} here. A reaction handler later
looks the reply up and nudges those lessons' confidence. JSON-in-overlay,
bounded to the most recent N replies.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_FILENAME = "feedback_map.json"
_MAX = 500


def _path() -> Path | None:
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    return Path(overlay) / _FILENAME if overlay else None


def _load() -> dict:
    path = _path()
    if not path or not path.exists():
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


def record(message_id: str, agent_id: str, lesson_ids: list[str]) -> None:
    """Remember which lessons a reply used (no-op if none)."""
    if not lesson_ids:
        return
    data = _load()
    data[str(message_id)] = {"agent_id": agent_id, "lessons": list(lesson_ids)}
    if len(data) > _MAX:  # prune oldest (dicts keep insertion order)
        for k in list(data)[: len(data) - _MAX]:
            del data[k]
    _save(data)


def get(message_id: str) -> dict | None:
    return _load().get(str(message_id))
