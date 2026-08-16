"""Cross-process reservations for tools that drive a single shared resource.

Some tools don't own their target. `chrome_browser` attaches over CDP to the one
real Chrome on the debug port and drives whatever tab it finds — so two agents
running concurrently steer the *same* tab. Agent A opens site X, agent B opens
site Y, A then screenshots and reads Y believing it is X. No exception is raised;
the tool returns confidently wrong data. This module makes agents take turns.

Each agent runs its own MCP-server process, so a module-level dict coordinates
nothing — the record lives on disk in a location every agent shares
($KBOTS_OVERLAY/data/reservations, falling back to the Core checkout's data/).
Mutations are serialised with fcntl.flock, held only across the read-modify-write
of a small JSON record, never across the tool's actual work.

A reservation goes free when any of these is true:

  * the holder released it explicitly (`release`),
  * it has not been used for RESERVATION_TTL seconds (default 10 min), or
  * the process that took it is gone.

Acquiring is never blocking: if someone else holds a live reservation, `acquire`
says so immediately and the caller redirects the agent elsewhere. Headless agents
cannot sit and wait on a lock.
"""

import logging
import os
import time
from dataclasses import dataclass

from src.core.locked_record import Locked, pid_alive, read_json, write_json

logger = logging.getLogger(__name__)

# A reservation nobody has used for this long is free for the taking. There is
# no end-of-turn hook in the engine, so idle expiry — not explicit release — is
# what guarantees the tool always comes back.
RESERVATION_TTL = float(os.environ.get("KBOTS_TOOL_RESERVATION_TTL", 600))

_NAMESPACE = "reservations"

# Kept as a module-level name so tests (and callers) can substitute it; the
# functions below deliberately look it up through the module.
_pid_alive = pid_alive


@dataclass(frozen=True)
class Reservation:
    """Who holds a resource, and since when."""
    resource: str
    agent_id: str
    pid: int
    acquired_at: float
    last_used_at: float

    def idle_for(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.last_used_at

    def expires_in(self, now: float | None = None, ttl: float | None = None) -> float:
        ttl = RESERVATION_TTL if ttl is None else ttl
        return max(0.0, ttl - self.idle_for(now))

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "pid": self.pid,
                "acquired_at": self.acquired_at, "last_used_at": self.last_used_at}


def _locked(resource: str) -> Locked:
    return Locked(_NAMESPACE, resource)


def _read(fd: int, resource: str) -> Reservation | None:
    """Parse the record from an open, locked fd. None if empty or corrupt."""
    data = read_json(fd)
    if data is None:
        return None
    try:
        return Reservation(resource=resource, agent_id=str(data["agent_id"]),
                           pid=int(data["pid"]), acquired_at=float(data["acquired_at"]),
                           last_used_at=float(data["last_used_at"]))
    except (KeyError, TypeError, ValueError):
        logger.warning(f"tool_reservation: discarding corrupt record for {resource}")
        return None


def _write(fd: int, res: Reservation | None) -> None:
    """Replace the record on an open, locked fd (None clears it)."""
    write_json(fd, None if res is None else res.to_dict())


def _is_stale(res: Reservation, now: float, ttl: float) -> bool:
    return res.idle_for(now) >= ttl or not _pid_alive(res.pid)


def acquire(resource: str, agent_id: str, *, pid: int | None = None,
            now: float | None = None,
            ttl: float | None = None) -> tuple[bool, Reservation]:
    """Reserve `resource` for `agent_id`, or report who has it.

    Never blocks on the reservation itself. Returns (True, ours) when the caller
    holds it — including the re-entrant case where it already did, in which case
    `last_used_at` is refreshed so an active session cannot expire mid-work.
    Returns (False, holder) when someone else holds a live one.
    """
    now = now if now is not None else time.time()
    ttl = RESERVATION_TTL if ttl is None else ttl
    pid = pid if pid is not None else os.getpid()

    with _locked(resource) as fd:
        current = _read(fd, resource)
        if current and current.agent_id != agent_id and not _is_stale(current, now, ttl):
            return False, current

        if current and current.agent_id == agent_id:
            # Re-entrant: same agent, possibly a new process. Keep the original
            # acquisition time, refresh the heartbeat and adopt the live pid.
            ours = Reservation(resource=resource, agent_id=agent_id, pid=pid,
                               acquired_at=current.acquired_at, last_used_at=now)
        else:
            if current:
                logger.info(
                    f"tool_reservation: taking over {resource} from '{current.agent_id}' "
                    f"(idle {int(current.idle_for(now))}s, pid alive: {_pid_alive(current.pid)})")
            ours = Reservation(resource=resource, agent_id=agent_id, pid=pid,
                               acquired_at=now, last_used_at=now)
        _write(fd, ours)
        return True, ours


def release(resource: str, agent_id: str) -> bool:
    """Give up the reservation. True if the caller actually held it.

    A non-holder calling this is a no-op — an agent must never be able to free
    someone else's live session out from under them.
    """
    with _locked(resource) as fd:
        current = _read(fd, resource)
        if not current or current.agent_id != agent_id:
            return False
        _write(fd, None)
        logger.info(f"tool_reservation: '{agent_id}' released {resource}")
        return True


def peek(resource: str, now: float | None = None,
         ttl: float | None = None) -> Reservation | None:
    """The current *live* holder, or None if free. Read-only — no side effects."""
    now = now if now is not None else time.time()
    ttl = RESERVATION_TTL if ttl is None else ttl
    with _locked(resource) as fd:
        current = _read(fd, resource)
    if not current or _is_stale(current, now, ttl):
        return None
    return current


def last_activity(resource: str) -> float | None:
    """`last_used_at` of the current record, live or stale — None if no record.

    Unlike peek() this deliberately reads expired records too: a reservation
    that ran out an hour ago is still evidence of when the resource was last
    driven, which is what idle-housekeeping (browser_janitor) needs.
    """
    with _locked(resource) as fd:
        current = _read(fd, resource)
    return current.last_used_at if current else None


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def busy_message(holder: Reservation, alternative: str = "", now: float | None = None,
                 ttl: float | None = None) -> str:
    """The refusal an agent sees when someone else holds the tool.

    Names the holder and when it frees up, so the agent can make a decision
    instead of retrying blindly.
    """
    msg = (f"⏳ {holder.resource} is reserved by '{holder.agent_id}' "
           f"(idle {format_duration(holder.idle_for(now))}, "
           f"expires in {format_duration(holder.expires_in(now, ttl))}). ")
    if alternative:
        msg += alternative + " "
    return msg + "Do not retry in a tight loop — wait or report back."
