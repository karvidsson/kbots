"""Browser janitor — idle detection, activity bookkeeping, quit decision."""

import time

import pytest

from src.core import browser_janitor, runtime_state, tool_reservation
from src.core.browser_janitor import _FLAG, _RESOURCE, BrowserJanitor


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


@pytest.fixture
def quiet_chrome(monkeypatch):
    """Pretend the debug Chrome is running and record close attempts."""
    closes = []

    async def fake_close(port=None):
        closes.append("cdp")
        return True

    monkeypatch.setattr(browser_janitor, "_port_up", lambda port=None: True)
    monkeypatch.setattr(browser_janitor, "_close_via_cdp", fake_close)
    return closes


def test_disabled_at_zero_hours():
    assert BrowserJanitor({"idle_quit_hours": 0}).enabled is False
    assert BrowserJanitor({}).enabled is True          # default 3h


async def test_first_sighting_never_kills(overlay, quiet_chrome):
    j = BrowserJanitor({"idle_quit_hours": 3})
    assert await j._tick() is False                    # initialises the window
    assert quiet_chrome == []
    assert runtime_state.get_flag(_FLAG) > 0


async def test_quits_after_idle_window(overlay, quiet_chrome):
    j = BrowserJanitor({"idle_quit_hours": 3})
    now = time.time()
    runtime_state.set_flag(_FLAG, now - 4 * 3600)      # last drive 4h ago
    assert await j._tick(now) is True
    assert quiet_chrome == ["cdp"]
    assert not runtime_state.get_flag(_FLAG)           # window reset after quit


async def test_not_yet_idle_enough(overlay, quiet_chrome):
    j = BrowserJanitor({"idle_quit_hours": 3})
    now = time.time()
    runtime_state.set_flag(_FLAG, now - 2 * 3600)
    assert await j._tick(now) is False
    assert quiet_chrome == []


async def test_live_reservation_blocks_quit(overlay, quiet_chrome):
    j = BrowserJanitor({"idle_quit_hours": 3})
    now = time.time()
    runtime_state.set_flag(_FLAG, now - 40 * 3600)     # ancient — would quit
    ok, _ = tool_reservation.acquire(_RESOURCE, "neon-husky", now=now)
    assert ok
    try:
        assert await j._tick(now) is False             # agent is driving
        assert quiet_chrome == []
        assert runtime_state.get_flag(_FLAG) == pytest.approx(now)
    finally:
        tool_reservation.release(_RESOURCE, "neon-husky")


async def test_stale_reservation_counts_as_activity(overlay, quiet_chrome):
    """An expired reservation still marks WHEN the browser was last driven."""
    j = BrowserJanitor({"idle_quit_hours": 3})
    now = time.time()
    ok, _ = tool_reservation.acquire(_RESOURCE, "a", now=now - 2 * 3600)
    assert ok                                          # 2h old → expired (TTL 10m)
    assert tool_reservation.peek(_RESOURCE) is None
    assert await j._tick(now) is False                 # 2h idle < 3h window
    assert quiet_chrome == []
    # ...but 4h later it quits
    assert await j._tick(now + 2 * 3600) is True
    assert quiet_chrome == ["cdp"]


async def test_port_down_resets_window(overlay, monkeypatch):
    monkeypatch.setattr(browser_janitor, "_port_up", lambda port=None: False)
    j = BrowserJanitor({"idle_quit_hours": 3})
    runtime_state.set_flag(_FLAG, time.time() - 40 * 3600)
    assert await j._tick() is False
    assert not runtime_state.get_flag(_FLAG)           # cleared: no Chrome running


async def test_sigterm_fallback_when_cdp_fails(overlay, monkeypatch):
    calls = []

    async def cdp_fails(port=None):
        return False

    async def fake_sigterm():
        calls.append("sigterm")

    monkeypatch.setattr(browser_janitor, "_port_up", lambda port=None: True)
    monkeypatch.setattr(browser_janitor, "_close_via_cdp", cdp_fails)
    monkeypatch.setattr(browser_janitor, "_close_via_sigterm", fake_sigterm)
    j = BrowserJanitor({"idle_quit_hours": 3})
    now = time.time()
    runtime_state.set_flag(_FLAG, now - 4 * 3600)
    assert await j._tick(now) is True
    assert calls == ["sigterm"]
