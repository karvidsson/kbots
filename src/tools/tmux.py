"""Tmux control tool — manage tmux sessions remotely.

For rescue bot / privileged agents. Allows listing, creating,
sending commands to, and reading from tmux sessions.
"""

import asyncio
import logging

from src.core.base import ToolContext
from src.core.tools import tool
from src.lib.clidep import cli_hint, resolve_cli

logger = logging.getLogger(__name__)


async def _tmux(args: str) -> str:
    """Run a tmux subcommand — resolves tmux via PATH or pkgx (zero-install)."""
    tm = resolve_cli("tmux")
    if not tm:
        return f"Error: {cli_hint('tmux')}"
    return await _run(f"{' '.join(tm)} {args}")


async def _run(cmd: str) -> str:
    """Run a shell command and return output."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        return f"Error: {stderr.decode().strip()}"
    return stdout.decode().strip()


@tool(name="tmux_list", description="List tmux sessions", category="system")
async def tmux_list(ctx: ToolContext) -> str:
    """List all active tmux sessions."""
    result = await _tmux("list-sessions 2>/dev/null")
    return result if result else "No tmux sessions"


@tool(name="tmux_send", description="Send a command to a tmux session", category="system")
async def tmux_send(ctx: ToolContext, session: str, command: str) -> str:
    """Send a command to a tmux session pane."""
    import shlex
    safe_cmd = shlex.quote(command)
    result = await _tmux(f"send-keys -t {shlex.quote(session)} {safe_cmd} Enter")
    if "Error" in result:
        return result

    # Wait briefly and capture output
    await asyncio.sleep(1)
    output = await _tmux(f"capture-pane -t {shlex.quote(session)} -p | tail -20")
    return output if output else "(sent, no visible output)"


@tool(name="tmux_read", description="Read current tmux pane content", category="system")
async def tmux_read(ctx: ToolContext, session: str, lines: int = 50) -> str:
    """Read the current visible content of a tmux pane."""
    import shlex
    result = await _tmux(f"capture-pane -t {shlex.quote(session)} -p | tail -{lines}")
    return result if result else "(empty pane)"


@tool(name="tmux_new", description="Create a new tmux session", category="system")
async def tmux_new(ctx: ToolContext, name: str, command: str = "") -> str:
    """Create a new tmux session, optionally running a command."""
    import shlex
    cmd = f"new-session -d -s {shlex.quote(name)}"
    if command:
        cmd += f" {shlex.quote(command)}"
    result = await _tmux(cmd)
    return result if result else f"Created tmux session: {name}"
