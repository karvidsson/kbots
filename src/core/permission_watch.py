"""Permission watch — detect permission failures at runtime and escalate.

Preflight (src/core/preflight.py) catches rights problems at boot, but the
worst ones appear while the service is running: an interactive `claude`
session run as root re-owns ~/.claude.json mid-day, workspace trust silently
stops applying, and every agent starts answering "I don't have permission"
until someone reads the service log. This module closes that gap:

  1. Engine-side failures (workspace-trust config unreadable) are reported
     the moment they happen via `notify()` from src/llm/claude_code.py.
  2. In-session tool denials ("you haven't granted it yet") are spotted in
     the CLI event stream and reported with the affected agent.
  3. A periodic sweep re-runs the preflight rights checks so damage done
     while agents are idle is caught within minutes, on any OS the service
     runs on — no cron jobs, launchd daemons, or systemd timers required.

Every report is deduplicated (per-issue cooldown) and escalated to the
configured **main agent**, which is woken with a structured briefing: what
broke, who is affected, the exact fix commands for this OS, and what kind of
access the fix needs (remote shell vs. a human at a browser or keyboard).
The agent verifies and reports to the owner in its channel. With no agent
configured, reports fall back to the security alert channel, then the log.

Config (Layer 3 config.yaml):

    security:
      permission_watch:
        enabled: true        # default true
        agent: ""            # main agent to brief (e.g. your coordinator agent)
        connector: discord   # where that agent should report
        channel: ""          # channel the briefing lands in (required for agent)
        interval: 300        # seconds between sweeps (min 60)
        cooldown: 3600       # seconds before the same issue is re-reported
"""

import asyncio
import getpass
import logging
import platform
import time
from pathlib import Path

from src.core.base import IncomingMessage

logger = logging.getLogger(__name__)

MIN_INTERVAL = 60

# The one denial signature that means "misconfiguration", not "a human said no".
# A user rejecting a HITL prompt or the CLI refusing a disallowed tool is
# working as intended; "you haven't granted it yet" on an agent's own allowed
# tool means the workspace trust / allow-list plumbing is broken.
_DENIAL_MARKER = "haven't granted it"

# Access levels a fix can require — surfaced verbatim in the briefing so the
# owner knows before they start whether SSH is enough.
ACCESS_SHELL = "remote shell (SSH) with sudo/admin rights"
ACCESS_HUMAN = "a human in an interactive terminal on the machine (SSH is OK)"
ACCESS_GUI = "a human at the machine's desktop (GUI/physical or screen sharing)"
ACCESS_WEB = "a web dashboard — no machine access needed"


def scan_stream_event(event: dict) -> list[str]:
    """Denial details found in one CLI stream-json event ([] = none).

    Tool denials arrive as `user` events carrying error tool_results whose
    text is e.g. "Claude requested permissions to write to <path>, but you
    haven't granted it yet." Only that marker is treated as a failure —
    explicit human rejections must not page anyone.
    """
    if event.get("type") != "user":
        return []
    found: list[str] = []
    for block in (event.get("message", {}) or {}).get("content", []) or []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        content = block.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(b.get("text", "") for b in content if isinstance(b, dict))
        for t in texts:
            if t and _DENIAL_MARKER in t.lower():
                found.append(t.strip()[:300])
    return found


def _fix_commands(kind: str, user: str, paths: list[str]) -> tuple[list[str], str]:
    """(fix command lines, access level) for an issue kind on this OS."""
    system = platform.system()
    joined = " ".join(paths) if paths else ""
    if kind in ("config_unreadable", "ownership"):
        if system == "Windows":
            # Native Windows has no chown; ownership fixes go through takeown/icacls.
            # (Supported deployments run the service under WSL2, where the Linux
            # commands apply — see docs/PERMISSIONS.md.)
            cmds = [f'takeown /F "{p}" /R' for p in paths] + \
                   [f'icacls "{p}" /grant {user}:(OI)(CI)F /T' for p in paths]
        else:
            cmds = [f"sudo chown -R {user} {joined}".rstrip()]
        return cmds, ACCESS_SHELL
    if kind == "tool_denied":
        # Denials are the *symptom*; the cause is nearly always an untrusted
        # workspace (unreadable claude config) or a missing/broken allow-list.
        home = str(Path.home())
        return [
            f"find {home}/.claude.json {home}/.claude -not -user {user}   # any output = the cause",
            f"sudo chown -R {user} {home}/.claude.json {home}/.claude",
            "then send the affected agent any message — trust re-heals on the next turn",
        ], ACCESS_SHELL
    return [], ACCESS_SHELL


class PermissionWatcher:
    """Collects permission failures, dedupes them, and briefs the main agent."""

    def __init__(self, agent_manager, config: dict, alerter=None):
        self.agent_manager = agent_manager
        self.config = config
        self.alerter = alerter
        pw = (config.get("security", {}) or {}).get("permission_watch", {}) or {}
        self.enabled = pw.get("enabled", True)
        self.agent = pw.get("agent", "")
        self.connector = pw.get("connector", "discord")
        self.channel = str(pw.get("channel", "") or "")
        self.interval = max(MIN_INTERVAL, int(pw.get("interval", 300)))
        self.cooldown = int(pw.get("cooldown", 3600))
        self._last_report: dict[str, float] = {}
        self._service_user = getpass.getuser()

    # --- reporting --------------------------------------------------------

    def report(self, kind: str, agent_id: str = "", detail: str = "",
               paths: list[str] | None = None) -> None:
        """Report a permission failure. Fire-and-forget, deduped, never raises.

        Safe to call from sync code inside the event loop (schedules a task).
        """
        if not self.enabled:
            return
        key = f"{kind}:{agent_id}:{','.join(paths or [])[:200]}"
        now = time.monotonic()
        if now - self._last_report.get(key, -self.cooldown) < self.cooldown:
            return
        self._last_report[key] = now
        briefing = self._briefing(kind, agent_id, detail, paths or [])
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.error(f"permission-watch (no loop): {briefing}")
            return
        asyncio.create_task(self._deliver(briefing), name="permission-watch-report")

    def _briefing(self, kind: str, agent_id: str, detail: str,
                  paths: list[str]) -> str:
        fixes, access = _fix_commands(kind, self._service_user, paths)
        titles = {
            "config_unreadable": "Claude Code config is unreadable — agents are "
                                 "losing ALL tools",
            "tool_denied": f"Agent '{agent_id or '?'}' had tool calls denied "
                           f"mid-session",
            "ownership": "Files owned by the wrong user detected",
        }
        lines = [
            "🔐 **Permission failure detected** (automated report from permission-watch)",
            f"**Issue:** {titles.get(kind, kind)}",
            f"**Host:** {platform.node()} ({platform.system()})",
        ]
        if agent_id:
            lines.append(f"**Affected agent:** {agent_id}")
        if detail:
            lines.append(f"**Detail:** {detail[:500]}")
        if fixes:
            lines.append("**Exact fix:**\n```\n" + "\n".join(fixes) + "\n```")
        lines.append(f"**Access required:** {access}")
        lines.append(
            "\nVerify this is still true (re-run the check in the detail above if "
            "one is shown), then report to the owner: what broke, which agents "
            "are affected, the exact commands to run, and whether SSH is enough "
            "or they need to be at the machine. If you have shell access and the "
            "fix is safe, apply it yourself and confirm the result instead.")
        return "\n".join(lines)

    async def _deliver(self, briefing: str) -> None:
        try:
            mgr = self.agent_manager
            if (self.agent and self.channel
                    and self.agent in getattr(mgr, "agent_configs", {})):
                routing = mgr.agent_configs[self.agent].get("routing", {})
                account = (routing.get(self.connector, {}) or {}).get("account")
                msg = IncomingMessage(
                    connector=self.connector,
                    channel_id=self.channel,
                    user_id="",
                    user_name="permission-watch",
                    content=briefing,
                    bot_account=account,
                )
                await mgr.handle_message(self.agent, msg)
                return
            if self.alerter:
                await self.alerter.send(briefing)
                return
        except Exception as e:  # escalation must never take the engine down
            logger.error(f"permission-watch: delivery failed: {e}")
        logger.error(f"permission-watch: {briefing}")

    # --- periodic sweep ---------------------------------------------------

    async def run(self) -> None:
        """Re-run the preflight rights checks forever; report what they find.

        Catches damage done while agents are idle (e.g. an interactive root
        `claude` session re-owning ~/.claude.json) within `interval` seconds,
        without any OS-specific timer machinery.
        """
        from src.core.preflight import _check_permissions
        logger.info(f"permission-watch: sweeping every {self.interval}s "
                    f"(escalation → {self.agent or 'alert channel' if self.alerter else 'log'})")
        while True:
            await asyncio.sleep(self.interval)
            try:
                for warning in _check_permissions(self.config):
                    self.report("ownership", detail=warning)
            except Exception as e:
                logger.warning(f"permission-watch: sweep failed: {e}")


# --- module-level hook so low-level code can report without plumbing --------

_watcher: PermissionWatcher | None = None


def set_watcher(watcher: PermissionWatcher | None) -> None:
    global _watcher
    _watcher = watcher


def notify(kind: str, agent_id: str = "", detail: str = "",
           paths: list[str] | None = None) -> None:
    """Report a permission failure if a watcher is installed (else no-op).

    Called from src/llm/claude_code.py at the moment a failure is seen.
    """
    if _watcher is not None:
        _watcher.report(kind, agent_id=agent_id, detail=detail, paths=paths)
