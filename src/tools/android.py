"""Android device tool — drive a phone or emulator over ADB.

Backend-agnostic: works against the headless emulator that
scripts/android-emulator.sh boots (auto-launched on first use) and, unchanged,
against a real Android phone connected over USB/Wi-Fi with USB debugging on.
When both are connected, a real phone is preferred; pin an explicit device
with KBOTS_ANDROID_SERIAL.

The control loop is screenshot-driven: capture the screen, look at it, then
tap/swipe/type at pixel coordinates. There is one shared device, so agents
take turns via the cross-process reservation (core/tool_reservation), same as
chrome_browser.

Emulator caveats worth knowing before automating real accounts: emulators
fail Play Integrity, social apps treat them with suspicion, and there is no
SIM (no incoming SMS). A physical device has none of these limits.
"""

import asyncio
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from pathlib import Path

from src.core import tool_reservation
from src.core.base import KBOTS_TMP, PROJECT_ROOT, ToolContext
from src.core.locked_record import Locked, pid_alive, read_json, write_json
from src.core.tools import tool

logger = logging.getLogger(__name__)

_DEFAULT_SDK = "/opt/homebrew/share/android-commandlinetools"
_LAUNCHER = PROJECT_ROOT / "scripts" / "android-emulator.sh"

# --- emulator lifecycle ---------------------------------------------------
#
# The emulator runs with -gpu swiftshader_indirect (software rendering), so it
# burns CPU on every frame and never settles into a cheap idle. Left running it
# is not a small leak: one abandoned instance measured 740% CPU — ~7.4 of 10
# cores — sustained for 32 hours, starving everything else on the box.
#
# Nothing shut it down because the launcher nohups the emulator and exits, so
# the process is orphaned to PID 1 and nobody owns it. We therefore record what
# we booted (pid + serial) and let a reaper in the service process shut it down
# once it has gone unused.
#
# Idle expiry cannot be checked lazily inside this tool: the failure mode *is*
# "nobody calls the tool again", which is exactly the case a lazy check never
# reaches. See EmulatorReaper at the bottom of this file.
_EMU_NAMESPACE = "managed"
_EMU_RECORD = "android_emulator"

# 20 minutes. A cold boot costs up to 180s (the timeout in _ensure_device), so
# reaping too eagerly makes an agent that steps away for a coffee pay three
# minutes on return; too lax and the machine cooks. 20min means at most ~3% of
# an idle hour is spent re-booting, while a forgotten emulator dies well inside
# the window where it would otherwise be noticed by the fans.
EMULATOR_IDLE_TIMEOUT = float(os.environ.get("KBOTS_ANDROID_IDLE_TIMEOUT", 1200))

# How often the reaper looks. Cheap (one file read), so frequent enough that
# actual shutdown lands close to the timeout.
EMULATOR_SWEEP_INTERVAL = float(os.environ.get("KBOTS_ANDROID_REAP_INTERVAL", 60))

# Grace given to each stage of shutdown before escalating.
_EMU_KILL_GRACE = 20.0

VALID_ACTIONS = ("status", "screenshot", "tap", "swipe", "type", "key",
                 "launch", "open_url", "install", "release")

RESOURCE = "android_device"
_FREE_ACTIONS = ("status", "release")  # don't touch the screen — no reservation
_ALTERNATIVE = ("Wait for the reservation to free up, or coordinate with the "
                "holding agent — there is only one Android device.")

_PACKAGE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")

# Friendly names for common keyevents; anything else can be passed as a raw
# KEYCODE_* name.
_KEYS = {
    "home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "enter": "KEYCODE_ENTER",
    "tab": "KEYCODE_TAB", "delete": "KEYCODE_DEL", "space": "KEYCODE_SPACE",
    "power": "KEYCODE_POWER", "wake": "KEYCODE_WAKEUP",
    "recents": "KEYCODE_APP_SWITCH", "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN", "paste": "KEYCODE_PASTE",
}


def _sdk_root() -> Path:
    return Path(os.environ.get("KBOTS_ANDROID_SDK", _DEFAULT_SDK))


def _adb_bin() -> str | None:
    sdk_adb = _sdk_root() / "platform-tools" / "adb"
    if sdk_adb.is_file():
        return str(sdk_adb)
    return shutil.which("adb")


def _avd_name() -> str:
    return os.environ.get("KBOTS_ANDROID_AVD", "kbots")


def _emu_read() -> dict | None:
    """The record of an emulator *this tool* booted, or None.

    None is the safe answer and the common one: a hand-started emulator or a
    physical phone never has a record, and so is never touched by the reaper.
    """
    with Locked(_EMU_NAMESPACE, _EMU_RECORD) as fd:
        data = read_json(fd)
    if not data:
        return None
    try:
        return {"pid": int(data["pid"]), "serial": str(data["serial"]),
                "avd": str(data.get("avd", "")),
                "started_at": float(data.get("started_at", 0.0)),
                "last_used_at": float(data["last_used_at"])}
    except (KeyError, TypeError, ValueError):
        logger.warning("android_device: discarding corrupt emulator record")
        return None


def _emu_write(pid: int, serial: str, avd: str, now: float | None = None) -> None:
    """Claim ownership of an emulator we just booted."""
    now = now if now is not None else time.time()
    with Locked(_EMU_NAMESPACE, _EMU_RECORD) as fd:
        write_json(fd, {"pid": pid, "serial": serial, "avd": avd,
                        "started_at": now, "last_used_at": now})
    logger.info(f"android_device: now owns emulator pid={pid} serial={serial} "
                f"(idle shutdown after {int(EMULATOR_IDLE_TIMEOUT)}s)")


def _emu_clear() -> None:
    with Locked(_EMU_NAMESPACE, _EMU_RECORD) as fd:
        write_json(fd, None)


def _emu_touch(serial: str, now: float | None = None) -> None:
    """Mark our emulator as used, so an active session is never reaped.

    Only refreshes when `serial` is the emulator we booted — driving a physical
    phone must not keep an unrelated idle emulator alive.
    """
    now = now if now is not None else time.time()
    with Locked(_EMU_NAMESPACE, _EMU_RECORD) as fd:
        data = read_json(fd)
        if not data or data.get("serial") != serial:
            return
        data["last_used_at"] = now
        write_json(fd, data)


async def _pid_command(pid: int) -> str:
    """The full command line of `pid`, or "" if it cannot be read."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-p", str(pid), "-o", "command=",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (OSError, asyncio.TimeoutError):
        return ""
    return out.decode(errors="replace").strip()


async def _is_our_emulator(pid: int, avd: str) -> bool:
    """True if `pid` really is the emulator we booted.

    PIDs are recycled, and this record can outlive a reboot by days — signalling
    a stale pid would kill an unrelated process. Confirm the command line still
    looks like the emulator for this AVD before touching it.
    """
    if not pid_alive(pid):
        return False
    cmd = await _pid_command(pid)
    if not cmd:
        return False
    looks_like_emulator = "qemu-system" in cmd or "/emulator" in cmd
    return looks_like_emulator and (not avd or f"-avd {avd}" in cmd or avd in cmd)


async def _run(*args: str, timeout: float = 30,
               binary: bool = False) -> tuple[int, bytes, str]:
    """Run adb with args. Returns (returncode, stdout, stderr-text)."""
    adb = _adb_bin()
    if not adb:
        return 127, b"", ("adb not found — install platform-tools "
                          "(see docs/ANDROID.md) or set KBOTS_ANDROID_SDK.")
    proc = await asyncio.create_subprocess_exec(
        adb, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, b"", f"adb {' '.join(args[:3])}… timed out after {int(timeout)}s"
    return proc.returncode or 0, out if binary else out.strip(), err.decode(errors="replace").strip()


async def _devices() -> list[str]:
    """Serials of devices in 'device' (fully attached) state."""
    rc, out, _ = await _run("devices")
    if rc != 0:
        return []
    serials = []
    for line in out.decode(errors="replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def _pick(serials: list[str]) -> str | None:
    pinned = os.environ.get("KBOTS_ANDROID_SERIAL")
    if pinned:
        return pinned if pinned in serials else None
    # Prefer a real phone over the emulator when both are attached.
    physical = [s for s in serials if not s.startswith("emulator-")]
    return (physical or serials or [None])[0]


async def _ensure_device(auto_launch: bool) -> tuple[str | None, str]:
    """Return (serial, error_message). Boots the emulator when needed."""
    serials = await _devices()
    serial = _pick(serials)
    if serial:
        return serial, ""
    if not auto_launch:
        return None, ("No Android device connected. Plug in a phone with USB "
                      "debugging, or retry with auto_launch=true to boot the "
                      "emulator.")
    if not _LAUNCHER.is_file():
        return None, f"No device, and the emulator launcher is missing: {_LAUNCHER}"
    logger.info("android_device: no device attached — booting the emulator")
    proc = await asyncio.create_subprocess_exec(
        "bash", str(_LAUNCHER),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        return None, "Emulator boot timed out after 180s — check its log in $TMPDIR."
    if proc.returncode != 0:
        tail = out.decode(errors="replace").strip().splitlines()[-3:]
        return None, "Emulator failed to boot: " + " / ".join(tail)
    serial = _pick(await _devices())
    if not serial:
        return None, "Emulator booted but no device appeared in adb."

    # Claim ownership only when this call actually booted something. The
    # launcher prints emulator_pid= on the boot path and exits early (without
    # it) when an emulator was already running — that one belongs to whoever
    # started it, and the reaper must leave it alone.
    stdout = out.decode(errors="replace")
    pid = _parse_emulator_pid(stdout)
    if pid and serial.startswith("emulator-"):
        _emu_write(pid, serial, _avd_name())
    elif "emulator_pid=" not in stdout:
        logger.info("android_device: emulator was already running — not claiming "
                    "ownership, so it will not be shut down automatically.")
    return serial, ""


def _parse_emulator_pid(stdout: str) -> int:
    for line in stdout.splitlines():
        if line.startswith("emulator_pid="):
            try:
                return int(line.split("=", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def _escape_text(text: str) -> str:
    """Escape text for `adb shell input text` (device-shell rules)."""
    out = []
    for ch in text:
        if ch == " ":
            out.append("%s")
        elif ch in "\\\"'`&|;<>()*~$#!":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


@tool(
    name="android_device",
    description=(
        "Control an Android device (headless emulator, or a real phone when one "
        "is plugged in) over ADB. Screenshot-driven: take a screenshot, read it, "
        "then tap/swipe/type at pixel coordinates. One shared device — it "
        "reserves itself while you use it; call action='release' when done. "
        "Actions: status, screenshot, tap, swipe, type, key, launch, open_url, "
        "install, release."
    ),
    category="android",
)
async def android_device(ctx: ToolContext, action: str, x: int = -1, y: int = -1,
                         x2: int = -1, y2: int = -1, text: str = "",
                         package: str = "", url: str = "", apk: str = "",
                         key: str = "", duration_ms: int = 300,
                         auto_launch: bool = True) -> str:
    """Drive an Android device over ADB.

    The workflow is look-then-act: 'screenshot' saves a PNG (open it with
    Read), decide the coordinates from what you see, then 'tap'/'swipe'/
    'type'. After every action, screenshot again to verify the result —
    Android UIs animate and your tap may have landed mid-transition.

    A headless emulator is booted automatically on first use if no device is
    attached (~1 min cold, seconds from snapshot). When a real phone is
    plugged in it is preferred automatically; everything works the same.

    SHARING: one device, agents take turns. The first driving action reserves
    it; others get a clear refusal until you call action='release' or go idle
    for 10 minutes. Release as soon as you're done.

    EMULATOR LIMITS: fails Play Integrity (some apps restrict or refuse),
    no SIM (cannot receive SMS codes), and social platforms treat emulator
    sessions as higher-risk — prefer a real phone before automating valuable
    accounts.

    Args:
        action: status, screenshot, tap, swipe, type, key, launch, open_url,
            install, release.
        x, y: pixel coordinates (tap; swipe start).
        x2, y2: swipe end coordinates.
        text: text to type into the focused field (for 'type').
        package: Android package name, e.g. 'com.android.settings' (launch).
        url: link to open with the default handler (open_url) — https:// or
            an app deep link.
        apk: path to an .apk file on this machine (install).
        key: for 'key' — home, back, enter, tab, delete, space, power, wake,
            recents, volume_up, volume_down, paste, or any raw KEYCODE_* name.
        duration_ms: swipe duration (default 300; longer = slower drag).
        auto_launch: boot the emulator if no device is attached (default true).
    """
    action = action.lower().strip()
    if action not in VALID_ACTIONS:
        return f"Unknown action: {action}. Valid: {', '.join(VALID_ACTIONS)}"

    if action == "release":
        freed = tool_reservation.release(RESOURCE, ctx.agent_id)
        return ("Released the android_device reservation — another agent can use it now."
                if freed else "You did not hold the android_device reservation.")

    if action == "status":
        serials = await _devices()
        serial = _pick(serials)
        if serial:
            # Checking on it counts as use — polling status must not let the
            # reaper pull the device out from under a watching agent.
            _emu_touch(serial)
        holder = tool_reservation.peek(RESOURCE)
        if holder and holder.agent_id != ctx.agent_id:
            who = (f"Reserved by '{holder.agent_id}' (idle "
                   f"{tool_reservation.format_duration(holder.idle_for())}, expires in "
                   f"{tool_reservation.format_duration(holder.expires_in())}).")
        elif holder:
            who = "Reserved by you."
        else:
            who = "Not reserved — free to use."
        if not serial:
            return (f"No Android device attached (emulator boots on first driving "
                    f"action). {who}")
        rc, out, _ = await _run("-s", serial, "shell", "wm", "size")
        size = out.decode(errors="replace").replace("Physical size:", "").strip() \
            if rc == 0 else "unknown"
        kind = "emulator" if serial.startswith("emulator-") else "physical device"
        extra = f" (+{len(serials) - 1} more attached)" if len(serials) > 1 else ""
        return f"Device: {serial} ({kind}){extra}. Screen: {size}. {who}"

    # --- driving actions: take the shared-device reservation ---
    try:
        ok, holder = tool_reservation.acquire(RESOURCE, ctx.agent_id)
    except Exception as e:
        return f"Could not check the android_device reservation: {e}"
    if not ok:
        return tool_reservation.busy_message(holder, _ALTERNATIVE)

    serial, err = await _ensure_device(auto_launch)
    if not serial:
        return err
    # Every driving action resets the idle clock, so an emulator in active use
    # is never reaped mid-session.
    _emu_touch(serial)
    dev = ("-s", serial)

    if action == "screenshot":
        media_dir = KBOTS_TMP / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="android_", suffix=".png", dir=str(media_dir))
        rc, out, err_txt = await _run(*dev, "exec-out", "screencap", "-p",
                                      binary=True, timeout=20)
        if rc != 0 or not out.startswith(b"\x89PNG"):
            os.close(fd)
            Path(tmp).unlink(missing_ok=True)
            return f"Screenshot failed: {err_txt or 'no PNG data from device'}"
        with os.fdopen(fd, "wb") as f:
            f.write(out)
        rc, size_out, _ = await _run(*dev, "shell", "wm", "size")
        size = size_out.decode(errors="replace").replace("Physical size:", "").strip() \
            if rc == 0 else ""
        return (f"Screenshot saved: {tmp}" + (f" (screen {size})" if size else "")
                + "\nOpen it with Read, then tap/swipe at the pixel coordinates you see.")

    if action == "tap":
        if x < 0 or y < 0:
            return "Error: 'tap' needs x and y pixel coordinates."
        rc, _, err_txt = await _run(*dev, "shell", "input", "tap", str(x), str(y))
        return f"Tapped ({x}, {y})." if rc == 0 else f"Tap failed: {err_txt}"

    if action == "swipe":
        if min(x, y, x2, y2) < 0:
            return "Error: 'swipe' needs x, y (start) and x2, y2 (end)."
        rc, _, err_txt = await _run(*dev, "shell", "input", "swipe",
                                    str(x), str(y), str(x2), str(y2),
                                    str(max(50, duration_ms)))
        return (f"Swiped ({x}, {y}) → ({x2}, {y2}) in {max(50, duration_ms)}ms."
                if rc == 0 else f"Swipe failed: {err_txt}")

    if action == "type":
        if not text:
            return "Error: 'type' needs text. Tap the input field first to focus it."
        rc, _, err_txt = await _run(*dev, "shell", "input", "text", _escape_text(text))
        return (f"Typed {len(text)} characters into the focused field."
                if rc == 0 else f"Type failed: {err_txt}")

    if action == "key":
        if not key:
            return f"Error: 'key' needs a key name. Known: {', '.join(sorted(_KEYS))}, or any KEYCODE_*."
        code = _KEYS.get(key.lower(), key if key.startswith("KEYCODE_") else "")
        if not code:
            return f"Unknown key '{key}'. Known: {', '.join(sorted(_KEYS))}, or any KEYCODE_*."
        rc, _, err_txt = await _run(*dev, "shell", "input", "keyevent", code)
        return f"Sent {code}." if rc == 0 else f"Keyevent failed: {err_txt}"

    if action == "launch":
        if not package or not _PACKAGE_RE.match(package):
            return "Error: 'launch' needs a valid package name, e.g. 'com.android.settings'."
        rc, out, err_txt = await _run(
            *dev, "shell", "monkey", "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1")
        txt = out.decode(errors="replace")
        if rc == 0 and "No activities found" not in txt:
            return f"Launched {package}. Screenshot to see where it landed."
        return (f"Could not launch {package} — is it installed? "
                f"{err_txt or txt.strip()[:200]}")

    if action == "open_url":
        if not url or ":" not in url:
            return "Error: 'open_url' needs a URL or app deep link."
        rc, _, err_txt = await _run(*dev, "shell", "am", "start",
                                    "-a", "android.intent.action.VIEW", "-d", url)
        return f"Opened {url}." if rc == 0 else f"open_url failed: {err_txt}"

    if action == "install":
        apk_path = Path(apk).expanduser()
        if not apk or not apk_path.is_file():
            return f"Error: 'install' needs the path to an existing .apk (got: '{apk}')."
        rc, out, err_txt = await _run(*dev, "install", "-r", str(apk_path), timeout=300)
        txt = out.decode(errors="replace")
        if rc == 0 and "Success" in txt:
            return f"Installed {apk_path.name}."
        return f"Install failed: {err_txt or txt.strip()[:300]}"

    return f"Unhandled action: {action}"  # unreachable — VALID_ACTIONS is exhaustive


# --- idle shutdown --------------------------------------------------------

async def shutdown_emulator(reason: str = "idle") -> str:
    """Shut down the emulator this tool booted. Returns a short outcome line.

    Safety rules, in order — each one is a case where doing nothing is correct:
      * no record        → we did not start it (hand-started, or a real phone)
      * pid not ours     → the pid died and was recycled; signalling would hit
                           an unrelated process
    Only after both pass do we signal, and never with SIGKILL: the emulator
    writes its AVD disk image on exit, and killing it mid-write corrupts the
    image the user's apps and logins live in.
    """
    rec = _emu_read()
    if not rec:
        return "no tool-owned emulator to shut down"

    pid, serial, avd = rec["pid"], rec["serial"], rec["avd"]

    if not await _is_our_emulator(pid, avd):
        logger.info(f"android_device: emulator pid={pid} is gone or no longer ours "
                    f"— clearing the record without signalling anything")
        _emu_clear()
        return f"stale record cleared (pid {pid} is not our emulator)"

    # 1. Ask the emulator to shut itself down. This is the clean path: it
    #    flushes the disk image and exits on its own.
    logger.info(f"android_device: shutting down emulator {serial} (pid {pid}) — {reason}")
    await _run("-s", serial, "emu", "kill", timeout=30)
    if await _await_exit(pid, _EMU_KILL_GRACE):
        _emu_clear()
        return f"emulator {serial} shut down gracefully ({reason})"

    # 2. SIGTERM — the emulator installs a handler and still saves state.
    # Re-verify the pid right before signalling: the grace window above is long
    # enough for the OS to recycle the pid onto an unrelated process.
    if not await _is_our_emulator(pid, avd):
        _emu_clear()
        return f"emulator {serial} exited during grace period ({reason})"
    logger.warning(f"android_device: '{serial} emu kill' did not exit within "
                   f"{int(_EMU_KILL_GRACE)}s — sending SIGTERM to pid {pid}")
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as e:
        logger.info(f"android_device: SIGTERM to {pid} failed ({e}) — treating as gone")
        _emu_clear()
        return f"emulator {serial} already gone"
    if await _await_exit(pid, _EMU_KILL_GRACE):
        _emu_clear()
        return f"emulator {serial} stopped via SIGTERM ({reason})"

    # 3. Deliberately no SIGKILL. Leave the record so the next sweep retries;
    #    a wedged emulator is a problem to surface, not to paper over with a
    #    kill that can corrupt the AVD.
    logger.error(f"android_device: emulator {serial} (pid {pid}) did not exit after "
                 f"emu kill and SIGTERM. NOT sending SIGKILL — that risks corrupting "
                 f"the AVD image. Investigate manually; will retry next sweep.")
    return f"emulator {serial} would not stop (left running, will retry)"


async def _await_exit(pid: int, timeout: float, poll: float = 0.5) -> bool:
    """Wait for `pid` to disappear. True if it did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        await asyncio.sleep(poll)
    return not pid_alive(pid)


async def reap_idle_emulator(now: float | None = None,
                             timeout: float | None = None) -> str:
    """One sweep: shut the emulator down if it has gone unused. Never raises."""
    now = now if now is not None else time.time()
    timeout = EMULATOR_IDLE_TIMEOUT if timeout is None else timeout
    rec = _emu_read()
    if not rec:
        return ""
    idle = now - rec["last_used_at"]
    if idle < timeout:
        return ""
    return await shutdown_emulator(
        reason=f"idle {tool_reservation.format_duration(idle)}")


class EmulatorReaper:
    """Shuts the emulator down when nobody is using it.

    This runs in the *service* process, not in an agent's tool call, and that
    is the whole point. Expiry checked lazily inside `android_device` would
    never fire for the case that actually hurts — an agent boots the emulator,
    wanders off, and never calls the tool again. The 32-hour 740%-CPU run that
    prompted this would have survived a lazy check untouched.
    """

    def __init__(self, interval: float | None = None,
                 timeout: float | None = None):
        self.interval = interval if interval is not None else EMULATOR_SWEEP_INTERVAL
        self.timeout = timeout if timeout is not None else EMULATOR_IDLE_TIMEOUT

    async def run(self) -> None:
        logger.info(f"Android emulator reaper: ON (idle shutdown after "
                    f"{int(self.timeout)}s, sweeping every {int(self.interval)}s)")
        while True:
            await asyncio.sleep(self.interval)
            try:
                outcome = await reap_idle_emulator(timeout=self.timeout)
                if outcome:
                    logger.info(f"Android emulator reaper: {outcome}")
            except Exception as e:  # a sweep must never kill the service task
                logger.warning(f"Android emulator reaper: sweep failed ({e})")
