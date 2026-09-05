"""Durable background jobs — work that outlives the session that started it.

Agents doing media work run builds that take minutes: a two-pass ffmpeg
assembly, a long render, a batch encode. Three things went wrong with running
those inline, all observed rather than theorised:

1. A foreground child dies with the session. The encode is killed mid-write,
   there is no output file, and the failure looks exactly like "still running"
   until someone stats the filesystem. An agent that had already said "it is
   building" then has to be asked whether it finished.
2. `nohup … & disown` does survive, but it is an incantation, and forgetting it
   fails silently. Worse on macOS: `setsid` DOES NOT EXIST here, so the usual
   `nohup setsid …` recipe fails instantly and writes nothing at all. That cost
   a real deploy. This module gets the same effect from `start_new_session=True`
   plus a double fork, neither of which needs a binary to be installed.
3. The harness's own background-task notification does not survive a restart:
   a job long enough to outlive the session is exactly the job whose completion
   never gets reported. So kbots keeps its own job state instead of borrowing
   the harness's.

DESIGN. One `jobs` table in the existing SQLite database, plain synchronous
`sqlite3` in WAL mode, because tools run in the MCP server process while the
watcher runs in the main process and the two share no connection. Same shape
the tool log already uses across that boundary.

THE EXIT FILE IS THE AUTHORITY, not the pid. The wrapper writes the exit status
to `<id>.exit` after the command returns, so a finished job is recognised even
if nothing was watching when it ended, including across a restart. Pid liveness
is only the fallback that catches a job killed before it could write one. That
ordering matters: pids are reused, so a liveness check alone can call a dead job
alive, and would be the one failure mode that reports work as still running when
it is gone.
"""

import asyncio
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_data_dir_override: Path | None = None

# Terminal states. `crashed` is distinct from `failed` on purpose: failed means
# the command ran and returned non-zero, crashed means it never got to say.
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CRASHED = "crashed"
CANCELLED = "cancelled"
TERMINAL = (DONE, FAILED, CRASHED, CANCELLED)

_LABEL_RE = re.compile(r"[^a-z0-9]+")
_MAX_TAIL = 200
_BUSY_RETRIES = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    label TEXT,
    command TEXT NOT NULL,
    cwd TEXT,
    pid INTEGER,
    status TEXT NOT NULL,
    exit_code INTEGER,
    log_path TEXT,
    connector TEXT,
    channel_id TEXT,
    bot_account TEXT,
    notify TEXT,
    notified INTEGER DEFAULT 0,
    started_at REAL,
    ended_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_agent ON jobs(agent_id, started_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


def set_data_dir(data_dir: Path | str | None) -> None:
    """Pin the location for readers in THIS process. Both entrypoints call it
    at boot; the MCP server is a separate process and must call it too."""
    global _data_dir_override
    _data_dir_override = Path(data_dir) if data_dir else None


def _data_dir() -> Path:
    return _data_dir_override if _data_dir_override else Path("./data")


def jobs_dir() -> Path:
    return _data_dir() / "jobs"


def _connect() -> sqlite3.Connection:
    """Open the shared database and ensure the table exists.

    Short-lived by design: two processes write here, and holding a connection
    open across an agent turn is how you collect `database is locked`.
    """
    from src.core.storage import resolve_db_path
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolve_db_path(d)))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _write(sql: str, params: tuple) -> None:
    """Write with a retry on lock. Mirrors the tool log's policy: a job record
    is not worth raising into an agent turn over a transient lock."""
    for attempt in range(_BUSY_RETRIES):
        conn = None
        try:
            conn = _connect()
            conn.execute(sql, params)
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < _BUSY_RETRIES - 1:
                time.sleep(0.1 * (attempt + 1))
                continue
            logger.error(f"jobs write failed: {e}")
            return
        finally:
            if conn:
                conn.close()


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _alive(pid: int | None) -> bool:
    """Is this pid still a live process we could signal?

    Only ever used to decide that a job with NO exit file has died. A reused pid
    therefore delays a crash verdict rather than inventing a completion.
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else. Still not gone.
        return True


def _slug(label: str) -> str:
    return _LABEL_RE.sub("-", label.lower()).strip("-")[:40]


def start(agent_id: str, command: str, *, cwd: str = "", label: str = "",
          connector: str = "", channel_id: str = "", bot_account: str = "",
          notify: str = "agent") -> dict:
    """Spawn `command` detached and record it. Returns the job row.

    The work ends up in its own session and orphaned onto init, so it survives
    the caller by construction rather than by remembering to type `nohup`.
    Raises OSError if the launcher does not hand back a pid.
    """
    job_id = f"{_slug(label) or 'job'}-{uuid.uuid4().hex[:8]}"
    d = jobs_dir()
    d.mkdir(parents=True, exist_ok=True)
    log_path = d / f"{job_id}.log"
    exit_path = d / f"{job_id}.exit"

    # A SUBSHELL around the command, not a brace group. `exit 3` inside braces
    # exits the wrapper shell itself, so the exit file is never written and a
    # job that failed cleanly reports as `crashed`. That is the difference
    # between "the build returned 3" and "we have no idea what happened".
    #
    # DOUBLE FORK, and this one is subtle. If the job is our direct child we
    # never reap it, so once it dies it becomes a ZOMBIE — and `kill(pid, 0)`
    # SUCCEEDS on a zombie, because the pid is still in the process table. A
    # killed job would then read as `running` forever, which is precisely the
    # lie this module exists to remove. So the launcher backgrounds the real
    # work and exits; the work is orphaned onto init, is nobody's child, and
    # cannot become a zombie. `echo $!` hands back the pid that matters.
    inner = (f"( ( {command} ) ; printf '%s' \"$?\" > {exit_path!s} ) "
             f"> {log_path!s} 2>&1 & echo $!")

    workdir = str(Path(cwd).expanduser()) if cwd else str(Path.cwd())
    launcher = subprocess.Popen(
        ["bash", "-lc", inner], cwd=workdir,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    # communicate() also reaps the launcher, so it leaves no zombie either.
    out, _ = launcher.communicate(timeout=30)
    try:
        pid = int(out.decode().strip().splitlines()[0])
    except (ValueError, IndexError, UnicodeDecodeError) as e:
        raise OSError(f"could not read job pid from launcher: {e}") from e

    row = {
        "id": job_id, "agent_id": agent_id, "label": label or job_id,
        "command": command, "cwd": workdir, "pid": pid, "status": RUNNING,
        "exit_code": None, "log_path": str(log_path), "connector": connector,
        "channel_id": channel_id, "bot_account": bot_account,
        "notify": notify, "notified": 0, "started_at": time.time(),
        "ended_at": None,
    }
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO jobs (id, agent_id, label, command, cwd, pid, status, "
            "exit_code, log_path, connector, channel_id, bot_account, notify, "
            "notified, started_at, ended_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["agent_id"], row["label"], row["command"], row["cwd"],
             row["pid"], row["status"], None, row["log_path"], row["connector"],
             row["channel_id"], row["bot_account"], row["notify"], 0,
             row["started_at"], None),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(f"job {job_id} started (pid {pid}) for {agent_id}: {command[:80]}")
    return row


def get(job_id: str) -> dict | None:
    rows = _rows("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return rows[0] if rows else None


def list_jobs(agent_id: str = "", limit: int = 20, running_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM jobs"
    where, params = [], []
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    if running_only:
        where.append("status = ?")
        params.append(RUNNING)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 100)))
    return _rows(sql, tuple(params))


def tail_log(job_id: str, lines: int = 40) -> str:
    job = get(job_id)
    if not job or not job.get("log_path"):
        return ""
    p = Path(job["log_path"])
    if not p.is_file():
        return ""
    n = max(1, min(int(lines), _MAX_TAIL))
    try:
        content = p.read_text(errors="replace").splitlines()
    except OSError as e:
        return f"(could not read log: {e})"
    return "\n".join(content[-n:])


def cancel(job_id: str) -> str:
    """Signal the job's whole process group, then mark it cancelled.

    The group, not the pid: a build script is a shell whose real work is a child
    ffmpeg, and killing only the shell leaves the encode running and holding the
    output file open.
    """
    job = get(job_id)
    if not job:
        return f"No job {job_id!r}."
    if job["status"] in TERMINAL:
        return f"Job {job_id} already {job['status']}."
    pid = job.get("pid")
    if not _alive(pid):
        _finish(job_id, CRASHED, None)
        return f"Job {job_id} was already gone; marked crashed."
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as e:
        return f"Could not signal job {job_id}: {e}"
    _finish(job_id, CANCELLED, None)
    return f"Job {job_id} cancelled (SIGTERM to process group)."


def _finish(job_id: str, status: str, exit_code: int | None) -> None:
    _write("UPDATE jobs SET status = ?, exit_code = ?, ended_at = ? WHERE id = ?",
           (status, exit_code, time.time(), job_id))


def mark_notified(job_id: str) -> None:
    _write("UPDATE jobs SET notified = 1 WHERE id = ?", (job_id,))


def _read_exit(job_id: str) -> int | None:
    p = jobs_dir() / f"{job_id}.exit"
    if not p.is_file():
        return None
    try:
        return int(p.read_text().strip() or -1)
    except (OSError, ValueError):
        return -1


def reconcile() -> list[dict]:
    """Settle every running job against reality. Returns newly terminal ones.

    Runs on a tick and at boot, so a job that ended while nothing was watching
    is still recognised. This is the call that turns "looks like it is still
    building" into a real answer.
    """
    settled: list[dict] = []
    for job in _rows("SELECT * FROM jobs WHERE status = ?", (RUNNING,)):
        code = _read_exit(job["id"])
        if code is not None:
            status = DONE if code == 0 else FAILED
            _finish(job["id"], status, code)
        elif not _alive(job.get("pid")):
            # No exit file and no process: killed before it could report.
            _finish(job["id"], CRASHED, None)
        else:
            continue
        fresh = get(job["id"])
        if fresh:
            settled.append(fresh)
    return settled


def summarise(job: dict) -> str:
    """One line a human or an agent can act on."""
    icon = {DONE: "✅", FAILED: "❌", CRASHED: "💥", CANCELLED: "🛑"}.get(job["status"], "▶️")
    dur = ""
    if job.get("started_at") and job.get("ended_at"):
        dur = f" in {int(job['ended_at'] - job['started_at'])}s"
    code = "" if job.get("exit_code") is None else f", exit {job['exit_code']}"
    return f"{icon} job `{job['id']}` {job['status']}{dur}{code}"


class JobWatcher:
    """Reconciles jobs on a tick and delivers completions.

    `notify` decides where a finished job lands:
      agent   — wake the owning agent with a synthetic message, so it can carry
                on (post the file, start the next pass) without being asked
      channel — post one line to the job's channel and stop there, no LLM turn
      none    — record it only; the agent finds it with job_list
    """

    def __init__(self, agent_manager, tick: float = 20.0):
        self.agent_manager = agent_manager
        self.tick = tick

    async def _post(self, job: dict, text: str) -> None:
        connector = (getattr(self.agent_manager, "connectors", {}) or {}).get(
            job.get("connector") or "discord")
        if not connector or not job.get("channel_id"):
            return
        try:
            await connector.send(job["channel_id"], text,
                                 bot_account=job.get("bot_account") or None)
        except Exception as e:
            logger.error(f"job notify post failed: {e}", exc_info=True)

    async def _wake(self, job: dict) -> None:
        from src.core.base import IncomingMessage
        mgr = self.agent_manager
        agent_id = job["agent_id"]
        if agent_id not in getattr(mgr, "agent_configs", {}):
            logger.warning(f"job {job['id']} → unknown agent '{agent_id}'")
            return
        tail = tail_log(job["id"], 30)
        msg = IncomingMessage(
            connector=job.get("connector") or "discord",
            channel_id=job.get("channel_id") or "",
            user_id="", user_name="jobs",
            content=(f"🧱 Background job finished: {summarise(job)}\n"
                     f"label: {job.get('label')}\n"
                     f"command: {job.get('command')}\n"
                     f"log: {job.get('log_path')}\n"
                     f"last lines:\n{tail}"),
            bot_account=job.get("bot_account") or None,
        )
        asyncio.create_task(mgr.handle_message(agent_id, msg), name=f"job-{job['id']}")

    async def deliver(self, job: dict) -> None:
        mode = (job.get("notify") or "agent").lower()
        if mode == "none":
            mark_notified(job["id"])
            return
        if mode == "channel":
            await self._post(job, summarise(job) + f"\n> {job.get('label')}")
            mark_notified(job["id"])
            return
        await self._wake(job)
        mark_notified(job["id"])

    async def run(self) -> None:
        logger.info("Job watcher started")
        while True:
            try:
                for job in reconcile():
                    logger.info(f"job settled: {summarise(job)}")
                    await self.deliver(job)
                # A job that went terminal while the process was down still has
                # notified=0, so completions survive a restart rather than being
                # lost with the session that was waiting for them.
                for job in _rows(
                        "SELECT * FROM jobs WHERE notified = 0 AND status != ?", (RUNNING,)):
                    await self.deliver(job)
            except Exception as e:
                logger.error(f"Job watcher tick failed: {e}", exc_info=True)
            await asyncio.sleep(self.tick)


def as_json(job: dict) -> str:
    return json.dumps(job, default=str, indent=2)
