"""Computer-control tools — let the agent drive the macOS machine it runs on.

Two owner/admin-only tools (macOS):
- computer: GUI control — screenshot, click, type, key, window/app management,
  and arbitrary AppleScript for higher-level app automation.
- run_command: run a shell command with a timeout and captured output.

Access: both are non-safe tools, so the sender-based access-control layer
(src/core/access_control.py) restricts them to owner/admin senders — staff,
unknown, and bot senders are dropped from the CLI's tools before the subprocess
spawns. That is the enforcement point; the tool runs in the MCP subprocess with
no caller identity, so there is no (and can't be a reliable) in-tool re-check.

macOS permissions: screenshots need Screen Recording; clicks/typing/window
control (System Events) need Accessibility. Grant them to the process running
kbots under System Settings → Privacy & Security. Every subprocess runs with
a hard timeout, so a missing permission returns actionable guidance instead of
hanging on a permission dialog.
"""

import asyncio
import logging
import os
import platform
import shlex
import tempfile
from pathlib import Path

from src.core.base import KBOTS_TMP, ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

_CMD_TIMEOUT = 20          # default per-subprocess timeout (seconds)

# Absolute paths, not bare names: the MCP subprocess the CLI spawns inherits a
# trimmed PATH that has no /usr/sbin, so `screencapture` resolved to
# FileNotFoundError ("command not found") even with Screen Recording granted.
# These are fixed macOS system locations — resolving them via PATH buys nothing.
_SCREENCAPTURE = "/usr/sbin/screencapture"
_OSASCRIPT = "/usr/bin/osascript"
_OPEN = "/usr/bin/open"
_SE_HINT = (
    "This needs macOS Accessibility permission. Grant it to the process running "
    "kbots: System Settings → Privacy & Security → Accessibility. "
    "(If it hung, a permission prompt is likely waiting on screen.)"
)
_SR_HINT = (
    "This needs macOS Screen Recording permission: System Settings → Privacy & "
    "Security → Screen Recording — enable it for the process running kbots."
)

COMPUTER_ACTIONS = ("screenshot", "click", "type", "key", "open_app",
                    "focus_app", "windows", "applescript", "perms")

# key aliases → AppleScript key codes (for non-printable keys)
_KEY_CODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "forwarddelete": 117,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
}
_MODIFIERS = {
    "cmd": "command down", "command": "command down", "ctrl": "control down",
    "control": "control down", "opt": "option down", "option": "option down",
    "alt": "option down", "shift": "shift down", "fn": "function down",
}


# Access is enforced upstream, in the main engine: the sender-based access
# control (src/core/access_control.py) marks these non-safe tools as
# owner/admin-only and drops them from the CLI's --disallowedTools for any
# staff/unknown/bot sender BEFORE the subprocess spawns. The tool itself can't
# re-check the caller — the MCP subprocess it runs in has neither the caller's
# user_id nor the AgentManager — so there is deliberately no in-tool tier check.


async def _run(args: list[str], timeout: int = _CMD_TIMEOUT,
               stdin: str | None = None) -> tuple[int | None, str, str]:
    """Run a command with a hard timeout. Returns (returncode, stdout, stderr).

    returncode is None on timeout (the process is killed).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", f"command not found: {args[0]}"
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None, "", "timeout"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def _osascript(script: str, timeout: int = _CMD_TIMEOUT) -> tuple[int | None, str, str]:
    return await _run([_OSASCRIPT, "-e", script], timeout=timeout)


def _se_error(rc: int | None, err: str) -> str | None:
    """Map a System Events failure to guidance, or None if it succeeded."""
    if rc is None:
        return f"Timed out. {_SE_HINT}"
    if rc != 0:
        low = err.lower()
        if "not allowed" in low or "assistive" in low or "1719" in err or "-25211" in err:
            return f"{err.strip()}\n{_SE_HINT}"
        return err.strip() or "System Events call failed."
    return None


@tool(
    name="computer",
    description=(
        "Control the macOS machine this agent runs on: screenshot the screen, click, "
        "type, press keys, manage windows/apps, or run AppleScript. Owner/admin only. "
        "Actions: screenshot, click, type, key, open_app, focus_app, windows, applescript, perms."
    ),
    category="system",
)
async def computer(ctx: ToolContext, action: str, x: int = 0, y: int = 0,
                   text: str = "", app: str = "", script: str = "",
                   button: str = "left", display: int = 0) -> str:
    """Drive the local macOS desktop (owner/admin only).

    Args:
        action: screenshot | click | type | key | open_app | focus_app | windows | applescript | perms
        x, y: screen coordinates (click).
        text: text to type (type), or key combo like "cmd+c"/"enter" (key).
        app: application name (open_app, focus_app).
        script: AppleScript source (applescript) — the most reliable way to
            automate apps (menus, dialogs, Finder, volume, notifications, …).
        button: "left" (default), "right", or "double" (click).
        display: display index for screenshot (0 = main/all).

    Screenshots need Screen Recording permission; clicks/typing/windows need
    Accessibility. Use 'perms' to check what's granted.
    """
    if platform.system() != "Darwin":
        return "computer is macOS-only (it drives the desktop via screencapture + osascript)."

    action = action.lower().strip()
    if action not in COMPUTER_ACTIONS:
        return f"Unknown action: {action}. Valid: {', '.join(COMPUTER_ACTIONS)}"
    logger.info(f"computer action={action} by user={ctx.user_id}")

    if action == "perms":
        media = KBOTS_TMP / "media"
        media.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(suffix=".png", dir=str(media))
        os.close(fd)
        rc, _, err = await _run([_SCREENCAPTURE, "-x", probe], timeout=10)
        screen_ok = rc == 0 and Path(probe).stat().st_size > 0
        Path(probe).unlink(missing_ok=True)
        # Short-timeout System Events probe; timeout ⇒ blocked on a prompt ⇒ not granted
        rc2, _, err2 = await _osascript(
            'tell application "System Events" to return name of first process', timeout=8)
        access_ok = rc2 == 0
        return (
            "macOS permission status:\n"
            f"- Screen Recording (screenshots): {'granted' if screen_ok else 'NOT granted — ' + _SR_HINT}\n"
            f"- Accessibility (click/type/windows): {'granted' if access_ok else 'NOT granted — ' + _SE_HINT}"
        )

    if action == "screenshot":
        media = KBOTS_TMP / "media"
        media.mkdir(parents=True, exist_ok=True)
        fd, out = tempfile.mkstemp(prefix="computer_screenshot_", suffix=".png", dir=str(media))
        os.close(fd)
        args = [_SCREENCAPTURE, "-x"]
        if display:
            args += ["-D", str(display)]
        args.append(out)
        rc, _, err = await _run(args, timeout=15)
        if rc != 0 or Path(out).stat().st_size == 0:
            Path(out).unlink(missing_ok=True)
            return f"Screenshot failed: {err.strip() or 'unknown'}\n{_SR_HINT}"
        return f"Screenshot saved: {out}"

    if action == "type":
        if not text:
            return "Error: 'text' required for type."
        rc, _, err = await _osascript(
            f'tell application "System Events" to keystroke {_as_str(text)}')
        guidance = _se_error(rc, err)
        return guidance or f"Typed {len(text)} character(s)."

    if action == "key":
        combo = text.strip().lower()
        if not combo:
            return "Error: 'text' required for key (e.g. 'cmd+c', 'enter')."
        parts = [p.strip() for p in combo.split("+") if p.strip()]
        mods = [_MODIFIERS[p] for p in parts if p in _MODIFIERS]
        keys = [p for p in parts if p not in _MODIFIERS]
        if len(keys) != 1:
            return f"Could not parse key combo '{text}'. Use e.g. 'cmd+shift+4', 'enter', 'tab'."
        base = keys[0]
        using = f" using {{{', '.join(mods)}}}" if mods else ""
        if base in _KEY_CODES:
            stmt = f"key code {_KEY_CODES[base]}{using}"
        elif len(base) == 1:
            stmt = f"keystroke {_as_str(base)}{using}"
        else:
            return f"Unknown key '{base}'. Known: {', '.join(sorted(_KEY_CODES))} or a single character."
        rc, _, err = await _osascript(f'tell application "System Events" to {stmt}')
        guidance = _se_error(rc, err)
        return guidance or f"Pressed {text}."

    if action == "click":
        btn = button.lower().strip()
        if btn == "right":
            # AppleScript's `click at` can't issue a right-click at raw
            # coordinates; right-click a specific UI element via applescript
            # (perform action "AXShowMenu"), or install cliclick for rc:x,y.
            return ("Right-click at coordinates isn't supported via osascript. "
                    "Use action='applescript' to AXShowMenu a specific element, "
                    "or left/double click.")
        if btn == "double":
            script_body = (f'tell application "System Events"\n'
                           f'  click at {{{x}, {y}}}\n  click at {{{x}, {y}}}\nend tell')
        else:
            script_body = f'tell application "System Events" to click at {{{x}, {y}}}'
        rc, _, err = await _osascript(script_body)
        guidance = _se_error(rc, err)
        return guidance or f"Clicked ({btn}) at ({x}, {y})."

    if action == "open_app":
        if not app:
            return "Error: 'app' required for open_app."
        rc, _, err = await _run([_OPEN, "-a", app], timeout=15)
        if rc != 0:
            return f"Could not open '{app}': {err.strip() or 'not found'}"
        return f"Opened {app}."

    if action == "focus_app":
        if not app:
            return "Error: 'app' required for focus_app."
        rc, _, err = await _osascript(f'tell application {_as_str(app)} to activate')
        if rc != 0:
            return f"Could not focus '{app}': {(err or '').strip()}"
        return f"Focused {app}."

    if action == "windows":
        script_body = (
            'tell application "System Events"\n'
            '  set out to ""\n'
            '  repeat with p in (every process whose background only is false)\n'
            '    set wc to count of windows of p\n'
            '    if wc > 0 then set out to out & name of p & " (" & wc & " window(s))\\n"\n'
            '  end repeat\n'
            '  return out\n'
            'end tell')
        rc, out, err = await _osascript(script_body, timeout=15)
        guidance = _se_error(rc, err)
        if guidance:
            return guidance
        return "Foreground apps with windows:\n" + (out.strip() or "(none)")

    if action == "applescript":
        if not script.strip():
            return "Error: 'script' required for applescript."
        rc, out, err = await _osascript(script, timeout=_CMD_TIMEOUT)
        if rc is None:
            return f"AppleScript timed out (>{_CMD_TIMEOUT}s). {_SE_HINT}"
        if rc != 0:
            guidance = _se_error(rc, err)
            return f"AppleScript error: {guidance or err.strip()}"
        return out.strip() or "AppleScript ran (no output)."

    return f"Action '{action}' not handled."


def _as_str(value: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@tool(
    name="run_command",
    description=(
        "Run a shell command on the macOS machine this agent runs on, with a timeout "
        "and captured output. Owner/admin only."
    ),
    category="system",
)
async def run_command(ctx: ToolContext, command: str, timeout: int = 60,
                      workdir: str = "") -> str:
    """Run a shell command and return its exit code, stdout, and stderr.

    Owner/admin only. Runs via `bash -lc`. Use for local automation the GUI
    actions don't cover (scripts, CLIs, file ops). `timeout` is capped at 600s.
    """
    if platform.system() != "Darwin":
        return "run_command is macOS-only in this tool."
    if not command.strip():
        return "Error: 'command' is empty."
    # Governance tripwire: permission files, credentials, and the agent
    # roster must not be modified through this tool — that path bypasses the
    # CLI permission layer entirely (observed live: an agent self-granting
    # MCP servers in its own settings.json). Ask the owner instead.
    sensitive_markers = (".claude/settings.json", "settings.local.json", "secrets.enc",
                  "kbots-vault-key", "k-agents-vault-key", "sudoers",
                  "agents.yaml", ".claude.json")
    lowered = command.lower()
    if any(marker in lowered for marker in sensitive_markers):
        return ("Blocked: this command references permission/credential files "
                f"({', '.join(m for m in sensitive_markers if m in lowered)}). Changes to "
                "those must go through the owner (scripts/settings.py) — tell the "
                "user what change you need and why, instead of applying it yourself.")
    timeout = max(1, min(int(timeout), 600))

    if workdir:
        wd = Path(workdir).expanduser()
        if not wd.is_dir():
            return f"workdir does not exist: {workdir}"
        full = f"cd {shlex.quote(str(wd))} && {command}"
    else:
        full = command

    logger.info(f"run_command by user={ctx.user_id}: {command[:120]}")
    rc, out, err = await _run(["bash", "-lc", full], timeout=timeout)
    if rc is None:
        return f"Command timed out after {timeout}s (process killed):\n{command}"

    parts = [f"exit code: {rc}"]
    if out.strip():
        parts.append("stdout:\n" + out.rstrip()[:8000])
    if err.strip():
        parts.append("stderr:\n" + err.rstrip()[:4000])
    return "\n\n".join(parts)
