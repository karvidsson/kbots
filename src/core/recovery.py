"""Interrupted-turn recovery — resume work lost to a restart.

A deploy restart drains in-flight turns for a bounded window; anything
longer is killed with the process, and nothing re-triggers it — the agent
keeps its session memory but never learns it was cut off. At shutdown the
turns still running when the drain window expires are snapshotted to disk;
on the next boot each affected agent gets one synthetic turn in the same
channel telling it to inspect its interrupted work and either finish it or
abstain (NO_REPLY replies are dropped, so silence stays silent).
"""

import json
import logging
import time
from pathlib import Path

from src.core.base import IncomingMessage

logger = logging.getLogger(__name__)

FILENAME = "interrupted_turns.json"

RECOVERY_PROMPT = (
    "⚠️ <restart-recovery>The platform restarted while you were mid-task in "
    "this channel, and that turn was killed before it finished. Review your "
    "recent messages here and your working state, then EITHER continue and "
    "finish the interrupted work now, OR — if it already completed, is no "
    "longer relevant, or is blocked on input you don't have — post a one-line "
    "status saying so. Do not claim any unverified action succeeded. If "
    "nothing needs saying, reply exactly NO_REPLY.</restart-recovery>"
)


def save_interrupted(data_dir: Path | str, turns: list[dict]) -> int:
    """Persist killed-turn records for the next boot. Returns how many.

    Internal (inter-agent loopback) channels are skipped — there is no real
    channel to post recovery into. One record per (agent, channel): a burst
    of queued messages in one channel is still one interrupted conversation.
    """
    seen: set[tuple[str, str]] = set()
    records = []
    for t in turns:
        channel = str(t.get("channel_id") or "")
        agent = str(t.get("agent_id") or "")
        if not agent or not channel or channel.startswith("internal:"):
            continue
        key = (agent, channel)
        if key in seen:
            continue
        seen.add(key)
        records.append(t)
    path = Path(data_dir) / FILENAME
    if not records:
        return 0
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved_at": time.time(), "turns": records}, indent=2))
        tmp.replace(path)
    except OSError as e:
        logger.error(f"Could not save interrupted turns: {e}")
        return 0
    return len(records)


def load_and_clear(data_dir: Path | str) -> list[dict]:
    """Read and remove the interrupted-turns file. Empty list when absent/bad."""
    path = Path(data_dir) / FILENAME
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        turns = data.get("turns", [])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Unreadable {FILENAME}: {e}")
        turns = []
    try:
        path.unlink()
    except OSError:
        pass
    return turns if isinstance(turns, list) else []


def build_recovery_message(turn: dict) -> IncomingMessage:
    """The synthetic turn delivered to the agent's channel after boot."""
    return IncomingMessage(
        connector=turn.get("connector", ""),
        channel_id=str(turn.get("channel_id", "")),
        user_id=str(turn.get("user_id") or ""),
        user_name="restart-recovery",
        content=RECOVERY_PROMPT,
        bot_account=turn.get("bot_account"),
    )
