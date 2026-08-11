"""Scheduled tasks — 'at <time>, have <agent> do <instruction>'.

The time-based companion to triggers.py (which is event-based). Stored as JSON
in the overlay so the MCP-tool process (which creates them) and the engine
(which fires them) share one source of truth.

A schedule is one of three spec types:
  - cron:  a 5-field cron expression, in server local time (recurring)
  - every: an interval in seconds (recurring)
  - once:  a unix timestamp; fires once then disables itself

Store format: {"enabled": bool, "schedules": [...]}. `enabled` is the global
killswitch for all scheduled inference.
"""

import contextlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_FILENAME = "schedules.json"


def _path() -> Path | None:
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    return Path(overlay) / _FILENAME if overlay else None


def _load_doc() -> dict:
    path = _path()
    if not path or not path.exists():
        return {"enabled": True, "schedules": []}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read schedules: {e}")
        return {"enabled": True, "schedules": []}
    if isinstance(data, dict):
        data.setdefault("enabled", True)
        data.setdefault("schedules", [])
        return data
    return {"enabled": True, "schedules": []}


def _save_doc(doc: dict) -> None:
    path = _path()
    if not path:
        raise RuntimeError("KBOTS_OVERLAY not set — cannot persist schedules")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")


# --- killswitch ---

def is_enabled() -> bool:
    return bool(_load_doc().get("enabled", True))


def set_enabled(enabled: bool) -> None:
    doc = _load_doc()
    doc["enabled"] = bool(enabled)
    _save_doc(doc)


# --- cron ---

def _field_matches(field: str, value: int, min_v: int, max_v: int) -> bool:
    """Match one cron field against a value. Supports *, N, A-B, A,B, */K, A-B/K."""
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) == 0:
                continue
            step = int(step_s)
        if part in ("*", ""):
            lo, hi = min_v, max_v
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                continue
            lo, hi = int(a), int(b)
        elif part.isdigit():
            lo = hi = int(part)
        else:
            continue
        if lo <= value <= hi and (value - lo) % step == 0:
            return True
    return False


def cron_matches(expr: str, dt: datetime) -> bool:
    """Does `dt` satisfy a 5-field cron expr (min hour dom month dow)?

    Day-of-week is 0-6 with Sunday=0 (7 also accepted for Sunday). dom and dow
    are ANDed (both must match) for predictability.
    """
    fields = expr.split()
    if len(fields) != 5:
        return False
    mn, hr, dom, mon, dow = fields
    dow = dow.replace("7", "0")
    wd = dt.isoweekday() % 7  # Sun=0, Mon=1..Sat=6
    return (
        _field_matches(mn, dt.minute, 0, 59)
        and _field_matches(hr, dt.hour, 0, 23)
        and _field_matches(dom, dt.day, 1, 31)
        and _field_matches(mon, dt.month, 1, 12)
        and _field_matches(dow, wd, 0, 6)
    )


def is_valid_cron(expr: str) -> bool:
    if len(expr.split()) != 5:
        return False
    # a spec that can never match any field is invalid
    try:
        return cron_matches(expr, datetime(2024, 1, 1)) in (True, False)
    except Exception:
        return False


# --- CRUD ---

def _next_id(items: list[dict]) -> str:
    nums = [int(s["id"][1:]) for s in items
            if s.get("id", "").startswith("s") and s["id"][1:].isdigit()]
    return f"s{(max(nums) + 1) if nums else 1}"


def create_schedule(
    agent_id: str,
    channel_id: str,
    instruction: str,
    created_by: str,
    *,
    spec_type: str,
    spec: str,
    now: float,
    connector: str = "discord",
    until: float = 0,
    max_runs: int = 0,
    skill: str = "",
    skill_params: dict | None = None,
    action: dict | None = None,
) -> dict:
    """Create a schedule. spec_type ∈ {cron, every, once}. Raises ValueError.

    until: unix timestamp after which the schedule auto-disables (0 = never).
    max_runs: disable after this many fires (0 = unlimited). Useful for
    watch-style recurring polls that should stop themselves.
    skill: fire a registered skill instead of a free-text agent turn (the
    skill's own llm/tool settings apply); skill_params fill its template.
    action: tool-direct binding {tool, args, narrate?, silent?} — executes ONE
    tool deterministically with no LLM turn at all (see src/core/actions.py).
    """
    if action is not None:
        from src.core.actions import validate_action
        err = validate_action(action)
        if err:
            raise ValueError(f"Invalid action: {err}")
    if not instruction.strip() and not action:
        raise ValueError("Instruction is required")
    if spec_type == "cron":
        if not is_valid_cron(spec):
            raise ValueError(f"Invalid cron expression {spec!r} (need 5 fields, e.g. '0 8 * * *')")
    elif spec_type == "every":
        if not str(spec).isdigit() or int(spec) < 30:
            raise ValueError("'every' must be an integer >= 30 seconds")
    elif spec_type == "once":
        if not str(spec).replace(".", "", 1).isdigit():
            raise ValueError("'once' must be a unix timestamp")
    else:
        raise ValueError("spec_type must be one of: cron, every, once")

    doc = _load_doc()
    record = {
        "id": _next_id(doc["schedules"]),
        "agent_id": agent_id,
        "channel_id": channel_id,
        "connector": connector,
        "instruction": instruction.strip(),
        "created_by": created_by,
        "spec_type": spec_type,
        "spec": str(spec),
        "enabled": True,
        "last_run": now,  # start the clock now; first 'every' fire is one interval out
        "until": float(until) if until else 0,
        "max_runs": int(max_runs) if max_runs else 0,
        "run_count": 0,
        "skill": skill or "",
        "skill_params": skill_params or {},
        "action": action or None,
    }
    doc["schedules"].append(record)
    _save_doc(doc)
    return record


def list_schedules(agent_id: str | None = None) -> list[dict]:
    items = _load_doc()["schedules"]
    return [s for s in items if not agent_id or s.get("agent_id") == agent_id]


def set_fields(schedule_id: str, **fields) -> bool:
    """Patch fields on a schedule record (e.g. board bookkeeping flags). Persists."""
    doc = _load_doc()
    for s in doc["schedules"]:
        if s.get("id") == schedule_id:
            s.update(fields)
            _save_doc(doc)
            return True
    return False


def delete_schedule(schedule_id: str) -> bool:
    doc = _load_doc()
    remaining = [s for s in doc["schedules"] if s.get("id") != schedule_id]
    if len(remaining) == len(doc["schedules"]):
        return False
    doc["schedules"] = remaining
    _save_doc(doc)
    return True


def _is_due(sch: dict, now: float) -> bool:
    until = sch.get("until") or 0
    if until and now >= until:
        return False  # expired — due_schedules disables it
    last = sch.get("last_run") or 0
    stype, spec = sch.get("spec_type"), sch.get("spec")
    if stype == "cron":
        dt = datetime.fromtimestamp(now)
        if not cron_matches(spec, dt):
            return False
        # one fire per matching minute: only if we haven't fired this minute
        minute_start = now - (dt.second + now % 1)
        return last < minute_start
    if stype == "every":
        return (now - last) >= int(spec)
    if stype == "once":
        return now >= float(spec)
    return False


def due_schedules(now: float) -> list[dict]:
    """Return schedules that should fire now, marking last_run (and disabling
    'once' schedules). Persists the updated state."""
    doc = _load_doc()
    if not doc.get("enabled", True):
        return []
    fired = []
    changed = False
    for sch in doc["schedules"]:
        if not sch.get("enabled", True):
            continue
        # Expire watch-style schedules whose window has passed, even if not due.
        until = sch.get("until") or 0
        if until and now >= until:
            sch["enabled"] = False
            changed = True
            continue
        if _is_due(sch, now):
            sch["last_run"] = now
            sch["run_count"] = int(sch.get("run_count", 0)) + 1
            max_runs = int(sch.get("max_runs", 0) or 0)
            if sch.get("spec_type") == "once" or (max_runs and sch["run_count"] >= max_runs):
                sch["enabled"] = False
            fired.append(dict(sch))
            changed = True
    if changed:
        with contextlib.suppress(Exception):
            _save_doc(doc)
    return fired
