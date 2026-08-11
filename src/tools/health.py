"""Health / status tools — system audit + running platform version."""

import asyncio
import os
import sys
import time
from pathlib import Path

from src.core import version
from src.core.base import ToolContext
from src.core.tools import tool


@tool(
    name="system_audit",
    description="Run a full system health audit against a deployment config file",
    category="ops",
)
async def system_audit(ctx: ToolContext) -> str:
    if sys.platform == "darwin":
        return (
            "system_audit is Linux-only (it relies on systemd, journalctl and "
            "GNU coreutils). On macOS check the service with: "
            "launchctl print gui/$UID/com.kbots.agent and the launchd logs "
            "in the overlay's data/ directory."
        )
    kbots_home = os.environ.get("KBOTS_HOME", "/opt/kbots")
    script = os.path.join(kbots_home, "scripts", "health-audit.sh")
    env = {**os.environ, "TERM": "dumb"}
    proc = await asyncio.create_subprocess_exec(
        script, "--report",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return "ERROR: health-audit.sh timed out (60s)"
    if proc.returncode != 0:
        err = stderr.decode().strip() if stderr else "unknown error"
        return f"ERROR: health-audit.sh failed (exit {proc.returncode}): {err}"
    return "Audit posted to #ops-alert"


def _fmt_age(booted_at: int) -> str:
    secs = max(0, int(time.time()) - int(booted_at or 0))
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h{m}m ago" if h else f"{m}m ago"


@tool(
    name="platform_version",
    description=(
        "Report which platform version is running vs what's on disk — use to "
        "confirm an update actually took effect (or that a restart is pending)."
    ),
    category="ops",
)
async def platform_version(ctx: ToolContext) -> str:
    running = version.read_running_version()
    checkout = version.current_commit()

    lines = ["**Platform version**"]
    if running:
        lines.append(
            f"- Running: **{running.get('version') or running.get('short', '?')}** "
            f"(`{running.get('short', '?')}`) — {running.get('subject', '')} "
            f"(booted {_fmt_age(running.get('booted_at', 0))})"
        )
    else:
        lines.append("- Running: unknown (no version recorded yet — a restart will record it)")
    lines.append(
        f"- On disk: **{checkout.get('version') or checkout['short']}** "
        f"(`{checkout['short']}`) — {checkout.get('subject', '')}"
    )

    if running and running.get("commit") and running["commit"] == checkout.get("commit"):
        lines.append("- Status: ✅ up to date (running matches the checkout)")
    elif running and running.get("commit"):
        lines.append(
            f"- Status: ⚠️ an update is on disk (`{checkout['short']}`) but NOT running yet — "
            f"restart to apply it (scripts/self-deploy.sh, or /admin reboot)."
        )

    overlay = os.environ.get("KBOTS_OVERLAY", "")
    if overlay:
        good = Path(overlay) / "data" / "last-good-commit"
        if good.exists():
            try:
                lines.append(f"- Last known-good deploy: `{good.read_text().strip()[:8]}`")
            except OSError:
                pass
    return "\n".join(lines)
