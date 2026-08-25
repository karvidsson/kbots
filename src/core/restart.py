"""Restarting the engine from inside the engine.

`/admin reboot` shelled out to `sudo systemctl restart kbots` on Linux. The
unit it ships sets `NoNewPrivileges=true`, which blocks setuid escalation
outright, so sudo cannot become root no matter what the sudoers file says:

    $ sudo -n systemctl restart kbots
    sudo: The "no new privileges" flag is set, which prevents sudo from
    running as root.

Both halves of that conflict ship in the same repo, and the handler was
fire-and-forget, so the failure went to an unread stderr while the user was
told "Restarting kbots... back in ~3 seconds." It is the documented way to
bring a newly provisioned bot online, and it never once worked on a hardened
install.

The supervisor is already willing to do this for free. systemd has
`Restart=always`, launchd has `KeepAlive`. A process that exits is restarted
within seconds, with no privileges required and no platform branch. So the
restart is: stop being a process.
"""

import asyncio
import logging
import os
import sys

logger = logging.getLogger(__name__)

# Long enough for the interaction response to reach Discord before the process
# goes away. An immediate exit loses the reply and the user sees a failed
# command for a restart that is in fact happening.
DEFAULT_DELAY = 1.5


def supervisor() -> str | None:
    """Which supervisor will restart this process, or None if nothing will.

    Reparenting to init is the signal both supervisors leave: launchd is pid 1
    on macOS, systemd is pid 1 on Linux. A process started by hand from a shell
    has its shell as parent, and exiting would simply stop the engine.
    """
    if os.getppid() != 1:
        return None
    if os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM"):
        return "systemd"          # set by systemd for every unit it starts
    return "launchd" if sys.platform == "darwin" else "systemd"


def restart_message() -> str:
    """What to tell the user, honestly, for this host."""
    sup = supervisor()
    if sup:
        return (f"Restarting kbots ({sup} will bring it back in a few seconds).")
    return ("NOT restarting: this process has no supervisor, so exiting would "
            "just stop it. Start kbots through its service (systemd/launchd) "
            "for /admin reboot to work, or restart it the way you started it.")


def can_restart() -> bool:
    return supervisor() is not None


async def restart_self(delay: float = DEFAULT_DELAY) -> None:
    """Exit, so the supervisor restarts us. Never returns.

    Scheduled rather than immediate so the caller can flush its reply first.
    Exit code 0: systemd's `Restart=always` and launchd's `KeepAlive` both
    restart regardless, and a non-zero code would make an ordinary restart look
    like a crash in the journal.
    """
    if not can_restart():
        raise RuntimeError(restart_message())
    logger.warning(f"Restart requested — exiting in {delay}s for {supervisor()} to restart us")
    await asyncio.sleep(delay)
    logger.warning("Exiting now")
    sys.exit(0)


def engine_root_writable() -> bool:
    """Whether this process can write into the engine checkout.

    False inside a hardened unit, where ProtectSystem=strict plus
    ReadOnlyPaths=<engine root> makes it read-only by design. `/admin update`
    ran `scripts/update.sh`, whose first act is a `git pull` there, so it could
    never work on such a host: the pull failed, the restart that followed hit
    the sudo wall above, and the command was non-functional as shipped while
    looking like an ordinary git failure.

    Probed by writing rather than by reading permission bits, because that is
    the thing the mount namespace changes and os.access does not see.
    """
    from src.core.base import PROJECT_ROOT

    probe = PROJECT_ROOT / ".kbots-write-probe"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False
