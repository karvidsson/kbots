"""Tiny JSON records shared across processes, guarded by fcntl.flock.

Every agent runs its own MCP-server process and the engine runs its own
service process, so a module-level dict coordinates nothing between them.
Anything those processes must agree on lives in a small JSON file under a
directory they all resolve identically, and every read-modify-write is
serialised with an exclusive flock held only for the duration of that
read-modify-write — never across the caller's actual work.

This module is mechanism, not policy. It knows how to store a dict safely and
whether a pid is alive; it has no opinion on what the dict means or when it
goes stale. Two callers use it with deliberately different semantics:

  * `tool_reservation` — *mutual exclusion*: who currently holds a shared tool,
    freed by idle expiry so a crashed agent cannot wedge it.
  * `src/tools/android` — *lifecycle ownership*: which OS process this tool
    booted, so a reaper can shut it down when nobody is using it.

Those are not the same concern and must not share one record (see the note in
tool_reservation), but they are the same plumbing, which is what lives here.
"""

import fcntl
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# How long to wait for the flock guarding a record. This only ever covers
# another process's read-modify-write, so it is short by construction; the
# timeout exists so a pathological holder degrades into an error, not a hang.
LOCK_TIMEOUT = 2.0
_LOCK_POLL = 0.02


def record_dir(namespace: str) -> Path:
    """The shared directory for `namespace` — every process must agree on it.

    $KBOTS_TMP is deliberately not used: it is per-agent and gets cleaned.
    """
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    if overlay:
        return Path(overlay) / "data" / namespace
    from src.core.base import PROJECT_ROOT
    return PROJECT_ROOT / "data" / namespace


def record_path(namespace: str, name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return record_dir(namespace) / f"{safe}.json"


def pid_alive(pid: int) -> bool:
    """True if the process still exists. Signal 0 checks without delivering."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def read_json(fd: int) -> dict | None:
    """Parse the record from an open, locked fd. None if empty or corrupt."""
    os.lseek(fd, 0, os.SEEK_SET)
    raw = b""
    while chunk := os.read(fd, 65536):
        raw += chunk
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def write_json(fd: int, data: dict | None) -> None:
    """Replace the record on an open, locked fd (None clears it)."""
    payload = b"" if data is None else (json.dumps(data) + "\n").encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    if payload:
        os.write(fd, payload)
    os.fsync(fd)


class Locked:
    """Open a record file and hold an exclusive flock for the block's duration.

    flock is released by the kernel if the holder dies, so a crashed process can
    never wedge the record — the worst case is a stale *record* inside it, which
    the caller's own staleness rules then clear.
    """

    def __init__(self, namespace: str, name: str):
        self.namespace = namespace
        self.name = name
        self.path = record_path(namespace, name)
        self.fd = -1

    def __enter__(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self.fd
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise TimeoutError(
                        f"Could not lock the {self.name} record "
                        f"within {LOCK_TIMEOUT}s.")
                time.sleep(_LOCK_POLL)

    def __exit__(self, *exc) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
