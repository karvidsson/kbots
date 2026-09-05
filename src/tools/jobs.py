"""Job tools — start work that outlives the session, then ask about it later.

`run_command` is the wrong instrument for a long build: it blocks the turn and
is hard-capped at 600 s, so a 15-minute ffmpeg assembly cannot complete through
it at all. These tools hand the work to src/core/jobs.py, which detaches it and
records it, and answer "what did I start, what happened to it, where is the
output" from durable state instead of from the agent's memory of a previous
session.

See src/core/jobs.py for why the exit file rather than the pid is the authority.
"""

import logging

from src.core import jobs
from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

NOTIFY_MODES = ("agent", "channel", "none")

# Same governance tripwire as run_command: a detached job is a shell, so it
# would otherwise be a clean way around the permission layer.
_SENSITIVE = (".claude/settings.json", "settings.local.json", "secrets.enc",
              "kbots-vault-key", "k-agents-vault-key", "sudoers",
              "agents.yaml", ".claude.json", "team.json")


@tool(name="job_start",
      description="Run a long command detached, surviving this session; notifies on completion",
      category="system")
async def job_start(ctx: ToolContext, command: str, label: str = "",
                    cwd: str = "", notify: str = "agent") -> str:
    """Start a long-running shell command as a durable background job.

    Use this for anything that takes minutes: media builds, renders, batch
    encodes, long test runs. The job is detached, so it keeps going after this
    session ends, and its completion is delivered even if that is several
    sessions later.

    notify:
      agent   — wake this agent when it finishes, with the log tail (default)
      channel — post one line to this channel, no further agent turn
      none    — record only; check it yourself with job_list

    Returns the job id. Ask about it later with job_status / job_logs.
    """
    if not command.strip():
        return "Error: 'command' is empty."
    if notify not in NOTIFY_MODES:
        return f"notify must be one of {', '.join(NOTIFY_MODES)}, not {notify!r}."

    lowered = command.lower()
    hits = [m for m in _SENSITIVE if m in lowered]
    if hits:
        return ("Blocked: this command references permission/credential files "
                f"({', '.join(hits)}). Detaching it would bypass the same gate "
                "run_command refuses to bypass. Ask the owner instead.")

    try:
        job = jobs.start(
            ctx.agent_id, command, cwd=cwd, label=label,
            connector="discord", channel_id=ctx.channel_id or "",
            notify=notify,
        )
    except OSError as e:
        return f"Could not start job: {e}"

    return (f"Job `{job['id']}` started (pid {job['pid']}) in {job['cwd']}.\n"
            f"Log: {job['log_path']}\n"
            f"It survives this session. Notify mode: {notify}. "
            f"Check it with job_status('{job['id']}').")


@tool(name="job_status", description="Status of one background job, with its log tail",
      category="system")
async def job_status(ctx: ToolContext, job_id: str, log_lines: int = 20) -> str:
    """Read a job's current state off durable storage.

    Reconciles first, so a job that finished while nothing was watching reports
    its real outcome rather than still saying running.
    """
    jobs.reconcile()
    job = jobs.get(job_id)
    if not job:
        return f"No job {job_id!r}. Use job_list to see recent ones."

    lines = [jobs.summarise(job),
             f"label:   {job.get('label')}",
             f"command: {job.get('command')}",
             f"cwd:     {job.get('cwd')}",
             f"log:     {job.get('log_path')}"]
    tail = jobs.tail_log(job_id, log_lines)
    if tail:
        lines.append(f"last {log_lines} lines:\n{tail}")
    return "\n".join(lines)


@tool(name="job_list", description="List recent background jobs for this agent",
      category="system")
async def job_list(ctx: ToolContext, limit: int = 10, running_only: bool = False,
                   all_agents: bool = False) -> str:
    """List jobs, most recent first. Reconciles before reporting."""
    jobs.reconcile()
    rows = jobs.list_jobs("" if all_agents else ctx.agent_id,
                          limit=limit, running_only=running_only)
    if not rows:
        return "No jobs recorded." if not running_only else "No jobs running."
    out = []
    for j in rows:
        out.append(f"{jobs.summarise(j)} · {j.get('label')} · {(j.get('command') or '')[:60]}")
    return "\n".join(out)


@tool(name="job_logs", description="Tail the log of a background job", category="system")
async def job_logs(ctx: ToolContext, job_id: str, lines: int = 60) -> str:
    """Read the tail of a job's combined stdout/stderr."""
    if not jobs.get(job_id):
        return f"No job {job_id!r}."
    tail = jobs.tail_log(job_id, lines)
    return tail or "(log is empty)"


@tool(name="job_cancel", description="Stop a running background job", category="system")
async def job_cancel(ctx: ToolContext, job_id: str) -> str:
    """Terminate a running job's whole process group and mark it cancelled."""
    return jobs.cancel(job_id)
