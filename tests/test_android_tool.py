"""android_device tool — ADB command construction, device pick, sharing."""

import signal
import time

import pytest

from src.core.base import ToolContext
from src.tools import android
from src.tools.android import _escape_text, _pick, android_device


@pytest.fixture
def adb_log(monkeypatch, tmp_path):
    """Record every adb invocation; each test scripts the responses."""
    calls = []
    responses = {}

    async def fake_run(*args, timeout=30, binary=False):
        calls.append(list(args))
        for needle, resp in responses.items():
            if needle in args:
                return resp
        return 0, b"", ""

    monkeypatch.setattr(android, "_run", fake_run)
    monkeypatch.setattr(android, "_adb_bin", lambda: "/fake/adb")
    # Reservations on a private tmp dir so tests never fight the real host
    monkeypatch.setattr(android.tool_reservation, "acquire",
                        lambda r, a, **k: (True, None))
    monkeypatch.setattr(android.tool_reservation, "peek", lambda r: None)
    monkeypatch.setattr(android.tool_reservation, "release", lambda r, a: True)
    # A connected device, so auto-launch never triggers
    responses["devices"] = (
        0, b"List of devices attached\nemulator-5554\tdevice", "")
    return calls, responses


def _ctx():
    return ToolContext(agent_id="tester")


# --- helpers ---

def test_escape_text_spaces_and_shell_chars():
    assert _escape_text("hi there") == "hi%sthere"
    assert _escape_text("a&b") == "a\\&b"
    assert _escape_text("it's") == "it\\'s"
    assert _escape_text("plain") == "plain"


def test_pick_prefers_physical_device(monkeypatch):
    monkeypatch.delenv("KBOTS_ANDROID_SERIAL", raising=False)
    assert _pick(["emulator-5554", "R58M12ABC"]) == "R58M12ABC"
    assert _pick(["emulator-5554"]) == "emulator-5554"
    assert _pick([]) is None


def test_pick_honors_pinned_serial(monkeypatch):
    monkeypatch.setenv("KBOTS_ANDROID_SERIAL", "R58M12ABC")
    assert _pick(["emulator-5554", "R58M12ABC"]) == "R58M12ABC"
    assert _pick(["emulator-5554"]) is None  # pinned device absent → no fallback


# --- actions ---

async def test_rejects_unknown_action(adb_log):
    out = await android_device(_ctx(), "explode")
    assert "Unknown action" in out


async def test_tap_requires_coordinates(adb_log):
    out = await android_device(_ctx(), "tap")
    assert "needs x and y" in out


async def test_tap_sends_input_tap(adb_log):
    calls, _ = adb_log
    out = await android_device(_ctx(), "tap", x=100, y=250)
    assert out == "Tapped (100, 250)."
    assert ["-s", "emulator-5554", "shell", "input", "tap", "100", "250"] in calls


async def test_swipe_enforces_min_duration(adb_log):
    calls, _ = adb_log
    await android_device(_ctx(), "swipe", x=0, y=0, x2=10, y2=10, duration_ms=5)
    assert ["-s", "emulator-5554", "shell", "input", "swipe",
            "0", "0", "10", "10", "50"] in calls


async def test_type_escapes_text(adb_log):
    calls, _ = adb_log
    await android_device(_ctx(), "type", text="hi there")
    assert ["-s", "emulator-5554", "shell", "input", "text", "hi%sthere"] in calls


async def test_key_maps_friendly_names_and_raw_keycodes(adb_log):
    calls, _ = adb_log
    await android_device(_ctx(), "key", key="home")
    assert ["-s", "emulator-5554", "shell", "input", "keyevent",
            "KEYCODE_HOME"] in calls
    out = await android_device(_ctx(), "key", key="KEYCODE_CAMERA")
    assert "Sent KEYCODE_CAMERA" in out
    out = await android_device(_ctx(), "key", key="frobnicate")
    assert "Unknown key" in out


async def test_launch_validates_package(adb_log):
    out = await android_device(_ctx(), "launch", package="not a package")
    assert "valid package name" in out


async def test_install_requires_existing_apk(adb_log, tmp_path):
    out = await android_device(_ctx(), "install", apk=str(tmp_path / "nope.apk"))
    assert "existing .apk" in out


async def test_screenshot_saves_png(adb_log, monkeypatch, tmp_path):
    calls, responses = adb_log
    monkeypatch.setattr(android, "KBOTS_TMP", tmp_path)
    responses["screencap"] = (0, b"\x89PNG\r\n\x1a\nfakedata", "")
    responses["wm"] = (0, b"Physical size: 1080x2400", "")
    out = await android_device(_ctx(), "screenshot")
    assert "Screenshot saved:" in out and "1080x2400" in out
    saved = list((tmp_path / "media").glob("android_*.png"))
    assert len(saved) == 1 and saved[0].read_bytes().startswith(b"\x89PNG")


async def test_screenshot_reports_failure_and_cleans_up(adb_log, monkeypatch, tmp_path):
    _, responses = adb_log
    monkeypatch.setattr(android, "KBOTS_TMP", tmp_path)
    responses["screencap"] = (1, b"", "device offline")
    out = await android_device(_ctx(), "screenshot")
    assert "Screenshot failed" in out and "device offline" in out
    assert list((tmp_path / "media").glob("android_*.png")) == []


# --- device acquisition ---

async def test_no_device_no_autolaunch_reports_clearly(adb_log):
    _, responses = adb_log
    responses["devices"] = (0, b"List of devices attached", "")
    out = await android_device(_ctx(), "tap", x=1, y=1, auto_launch=False)
    assert "No Android device connected" in out


async def test_busy_reservation_refuses_driving_action(adb_log, monkeypatch):
    holder = android.tool_reservation.Reservation(
        resource=android.RESOURCE, agent_id="other", pid=1,
        acquired_at=0.0, last_used_at=9e12)
    monkeypatch.setattr(android.tool_reservation, "acquire",
                        lambda r, a, **k: (False, holder))
    out = await android_device(_ctx(), "tap", x=1, y=1)
    assert "reserved by 'other'" in out


async def test_status_and_release_skip_reservation(adb_log, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("free actions must not acquire")
    monkeypatch.setattr(android.tool_reservation, "acquire", boom)
    out = await android_device(_ctx(), "status")
    assert "Device: emulator-5554 (emulator)" in out
    out = await android_device(_ctx(), "release")
    assert "Released" in out


# --- emulator idle shutdown ------------------------------------------------

@pytest.fixture
def emu_record(monkeypatch, tmp_path):
    """Point the ownership record at a private dir, and stub process control."""
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    killed = {"sigterm": [], "emu_kill": []}
    alive = {"pids": set()}

    def fake_pid_alive(pid):
        return pid in alive["pids"]

    async def fake_pid_command(pid):
        return ("/opt/android/emulator/qemu/qemu-system-aarch64-headless "
                "-avd kbots -no-window") if pid in alive["pids"] else ""

    def fake_kill(pid, sig):
        killed["sigterm"].append((pid, sig))
        alive["pids"].discard(pid)          # a well-behaved emulator exits

    monkeypatch.setattr(android, "pid_alive", fake_pid_alive)
    monkeypatch.setattr(android, "_pid_command", fake_pid_command)
    monkeypatch.setattr(android.os, "kill", fake_kill)
    monkeypatch.setattr(android, "_EMU_KILL_GRACE", 0.2)
    return killed, alive


def _own_emulator(pid=4242, serial="emulator-5554", last_used=None, alive=None):
    android._emu_write(pid, serial, "kbots")
    if last_used is not None:
        with android.Locked(android._EMU_NAMESPACE, android._EMU_RECORD) as fd:
            data = android.read_json(fd)
            data["last_used_at"] = last_used
            android.write_json(fd, data)
    if alive is not None:
        alive["pids"].add(pid)


async def test_reaper_fires_with_no_caller_present(emu_record, monkeypatch):
    """The whole point: shutdown happens without anyone calling android_device."""
    killed, alive = emu_record
    emu_kill = []

    async def fake_run(*args, timeout=30, binary=False):
        if "emu" in args:
            emu_kill.append(list(args))
            alive["pids"].discard(4242)     # emu kill works
        return 0, b"", ""

    monkeypatch.setattr(android, "_run", fake_run)
    _own_emulator(last_used=time.time() - 5000, alive=alive)

    out = await android.reap_idle_emulator()
    assert "shut down gracefully" in out
    assert ["-s", "emulator-5554", "emu", "kill"] in emu_kill
    assert android._emu_read() is None      # record cleared


async def test_in_use_emulator_is_not_killed(emu_record, monkeypatch):
    killed, alive = emu_record

    async def boom(*a, **k):
        raise AssertionError("must not signal an emulator still in use")

    monkeypatch.setattr(android, "_run", boom)
    _own_emulator(last_used=time.time() - 60, alive=alive)   # idle 1 min

    assert await android.reap_idle_emulator() == ""
    assert android._emu_read() is not None


async def test_hand_started_emulator_is_never_touched(emu_record, monkeypatch):
    """No ownership record => someone else started it => leave it alone."""
    async def boom(*a, **k):
        raise AssertionError("must not signal an emulator we did not start")

    monkeypatch.setattr(android, "_run", boom)
    assert android._emu_read() is None
    assert await android.reap_idle_emulator() == ""
    assert await android.shutdown_emulator() == "no tool-owned emulator to shut down"


async def test_physical_device_is_never_recorded(adb_log, emu_record, monkeypatch):
    """A real phone must never end up owned — nothing to shut down."""
    _, responses = adb_log
    monkeypatch.delenv("KBOTS_ANDROID_SERIAL", raising=False)
    responses["devices"] = (0, b"List of devices attached\nR58M12ABC\tdevice", "")
    await android_device(_ctx(), "tap", x=1, y=1)
    assert android._emu_read() is None


async def test_sigterm_fallback_when_emu_kill_does_nothing(emu_record, monkeypatch):
    killed, alive = emu_record

    async def fake_run(*args, timeout=30, binary=False):
        return 0, b"", ""                   # emu kill silently does nothing

    monkeypatch.setattr(android, "_run", fake_run)
    _own_emulator(last_used=time.time() - 5000, alive=alive)

    out = await android.reap_idle_emulator()
    assert "SIGTERM" in out
    assert killed["sigterm"] == [(4242, signal.SIGTERM)]
    assert android._emu_read() is None


async def test_never_sigkills_a_wedged_emulator(emu_record, monkeypatch):
    """A wedged emulator is left running: SIGKILL risks corrupting the AVD."""
    killed, alive = emu_record

    async def fake_run(*args, timeout=30, binary=False):
        return 0, b"", ""

    def stubborn_kill(pid, sig):
        killed["sigterm"].append((pid, sig))  # ignores the signal, stays alive

    monkeypatch.setattr(android, "_run", fake_run)
    monkeypatch.setattr(android.os, "kill", stubborn_kill)
    _own_emulator(last_used=time.time() - 5000, alive=alive)

    out = await android.reap_idle_emulator()
    assert "would not stop" in out
    assert signal.SIGKILL not in [s for _, s in killed["sigterm"]]
    assert android._emu_read() is not None   # kept, so the next sweep retries


async def test_recycled_pid_is_not_signalled(emu_record, monkeypatch):
    """The pid died and was reused by something else — clear, don't kill."""
    killed, alive = emu_record

    async def unrelated(pid):
        return "/usr/bin/postgres -D /var/db"   # alive, but not our emulator

    async def boom(*a, **k):
        raise AssertionError("must not adb into a recycled pid")

    monkeypatch.setattr(android, "_pid_command", unrelated)
    monkeypatch.setattr(android, "_run", boom)
    _own_emulator(last_used=time.time() - 5000, alive=alive)

    out = await android.shutdown_emulator()
    assert "stale record" in out
    assert killed["sigterm"] == []
    assert android._emu_read() is None


async def test_touch_refreshes_only_our_serial(emu_record):
    _own_emulator(serial="emulator-5554", last_used=1000.0)
    android._emu_touch("R58M12ABC", now=9000.0)          # a phone
    assert android._emu_read()["last_used_at"] == 1000.0
    android._emu_touch("emulator-5554", now=9000.0)      # our emulator
    assert android._emu_read()["last_used_at"] == 9000.0


async def test_driving_action_refreshes_idle_clock(adb_log, emu_record):
    _own_emulator(serial="emulator-5554", last_used=1000.0)
    await android_device(_ctx(), "tap", x=1, y=1)
    assert android._emu_read()["last_used_at"] > 1000.0


def test_parses_emulator_pid_from_launcher_output():
    assert android._parse_emulator_pid(
        "booting AVD 'kbots' headless\nemulator_pid=8123\nboot complete") == 8123
    # "already running" path prints no pid — ownership must not be claimed
    assert android._parse_emulator_pid("emulator already running") == 0


async def test_reaper_loop_sweeps_on_its_own(monkeypatch):
    """EmulatorReaper.run() must drive sweeps unprompted, and survive failures."""
    import asyncio
    sweeps = []

    async def fake_reap(now=None, timeout=None):
        sweeps.append(timeout)
        if len(sweeps) == 2:
            raise RuntimeError("transient adb failure")
        return "emulator shut down gracefully (idle)" if len(sweeps) == 1 else ""

    monkeypatch.setattr(android, "reap_idle_emulator", fake_reap)
    reaper = android.EmulatorReaper(interval=0.01, timeout=1200)
    task = asyncio.create_task(reaper.run())
    await asyncio.sleep(0.15)
    still_running = not task.done()
    task.cancel()

    assert sweeps[0] == 1200, "configured timeout should reach the sweep"
    # sweep 2 raised. Both of these must hold for the reaper to be trustworthy:
    # the loop carried on sweeping, and the exception did not kill the task.
    assert len(sweeps) >= 3, "a failed sweep must not stop the loop"
    assert still_running, "a failed sweep must not kill the reaper task"


def test_idle_timeout_is_env_overridable(monkeypatch):
    import importlib
    monkeypatch.setenv("KBOTS_ANDROID_IDLE_TIMEOUT", "300")
    reloaded = importlib.reload(android)
    try:
        assert reloaded.EMULATOR_IDLE_TIMEOUT == 300
    finally:
        monkeypatch.delenv("KBOTS_ANDROID_IDLE_TIMEOUT", raising=False)
        importlib.reload(android)
