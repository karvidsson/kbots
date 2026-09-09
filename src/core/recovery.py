"""Interrupted-turn recovery — resume work lost to a restart.

A deploy restart drains in-flight turns for a bounded window; anything
longer is killed with the process, and nothing re-triggers it — the agent
keeps its session memory but never learns it was cut off. At shutdown the
turns still running when the drain window expires are snapshotted to disk;
on the next boot each affected agent gets one synthetic turn in the same
channel telling it to inspect its interrupted work and either finish it or
abstain (NO_REPLY replies are dropped, so silence stays silent).
"""

import asyncio
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


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        turns = data.get("turns", [])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Unreadable {FILENAME}: {e}")
        return []
    return turns if isinstance(turns, list) else []


def load_pending(data_dir: Path | str) -> list[dict]:
    """Read the interrupted-turns file without consuming it.

    The file used to be read and deleted in one step, and delivery ran the
    turns one after another. A long first recovery turn then held the rest,
    and a second restart inside that window found no file and dropped them:
    the owner's question in a goal channel was never answered because one
    engineer's recovery ran for twenty minutes ahead of it. Records now stay
    on disk until each one has been handed to its agent.
    """
    path = Path(data_dir) / FILENAME
    turns = _read(path)
    if not turns and path.exists():
        # unreadable or empty: nothing to recover, do not keep tripping on it
        try:
            path.unlink()
        except OSError:
            pass
    return turns


def load_and_clear(data_dir: Path | str) -> list[dict]:
    """Read and remove the interrupted-turns file. Empty list when absent/bad."""
    path = Path(data_dir) / FILENAME
    turns = _read(path)
    try:
        path.unlink()
    except OSError:
        pass
    return turns


_file_lock = asyncio.Lock()


def _same(a: dict, b: dict) -> bool:
    return (str(a.get("agent_id")), str(a.get("channel_id"))) == \
        (str(b.get("agent_id")), str(b.get("channel_id")))


async def mark_delivered(data_dir: Path | str, turn: dict) -> None:
    """Drop one record from the file; delete the file when it is the last."""
    path = Path(data_dir) / FILENAME
    async with _file_lock:
        remaining = [t for t in _read(path) if not _same(t, turn)]
        try:
            if remaining:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"saved_at": time.time(), "turns": remaining},
                                          indent=2))
                tmp.replace(path)
            elif path.exists():
                path.unlink()
        except OSError as e:
            logger.error(f"Could not update {FILENAME}: {e}")


async def deliver_all(agent_manager, data_dir: Path | str, turns: list[dict],
                      delay: float = 20.0) -> None:
    """Hand every interrupted turn to its agent, all at once.

    Concurrent on purpose: recovery turns are independent conversations, and
    running them in sequence let one slow agent starve the rest. Each record
    is removed from the file only once its agent has been given the turn, so
    a restart mid-way re-delivers whatever had not been reached. The killed
    turn itself is then in flight again and the shutdown path re-records it.
    """
    await asyncio.sleep(delay)  # let connectors finish coming online

    async def _one(turn: dict) -> None:
        agent_id = turn.get("agent_id")
        if agent_id not in getattr(agent_manager, "agent_configs", {}):
            logger.warning(f"Restart recovery: unknown agent {agent_id!r} — skipped")
            await mark_delivered(data_dir, turn)
            return
        logger.info(f"Restart recovery → {agent_id} in {turn.get('channel_id')}")
        try:
            await agent_manager.handle_message(agent_id, build_recovery_message(turn))
        except Exception as e:
            logger.error(f"Restart recovery for {agent_id} failed: {e}")
        finally:
            await mark_delivered(data_dir, turn)

    await asyncio.gather(*(_one(t) for t in turns))


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
