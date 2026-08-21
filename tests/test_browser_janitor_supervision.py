"""The janitor must not close a browser it did not launch on a stale verdict.

Two failures, found 2026-08-20 while diagnosing a debug Chrome that would not
stay up for ten minutes:

1. `_last_activity` consulted the chrome_browser reservation record, which
   OUTLIVES the browser it describes. A freshly launched Chrome was therefore
   dated to the last time the tool ran — days earlier — and closed on the next
   tick. Anything driving the port over raw CDP writes no reservation at all,
   so that verdict never changed: every launch died within one tick, forever.
   The live log showed the idle counter climbing past 2900 minutes while the
   browser was relaunched by hand several times a day.

2. Once the browser is a KeepAlive launchd job, quitting it at all is wrong.
   launchd restarts it within seconds and the two fight in a loop, so every CDP
   consumer sees an endpoint that dies at random.
"""

import asyncio
import time

import pytest

from src.core import browser_janitor as bj

DAYS = 86400 * 3


@pytest.fixture
def janitor(monkeypatch):
    """A janitor over a live port, with the store and the killer stubbed out."""
    flags: dict = {}
    closed: list = []
    monkeypatch.setattr(bj, "_port_up", lambda *a, **k: True)
    monkeypatch.setattr(bj.runtime_state, "get_flag",
                        lambda name, default=None: flags.get(name, default))
    monkeypatch.setattr(bj.runtime_state, "set_flag",
                        lambda name, value: flags.__setitem__(name, value))
    monkeypatch.setattr(bj.tool_reservation, "peek", lambda r: None)
    monkeypatch.setattr(bj, "_supervised", lambda: False)

    async def fake_close(*a, **k):
        closed.append(True)
        return True

    monkeypatch.setattr(bj, "_close_via_cdp", fake_close)
    return bj.BrowserJanitor({"idle_quit_hours": 3}), flags, closed, monkeypatch


def test_a_newly_seen_browser_is_never_closed_on_the_first_tick(janitor, monkeypatch):
    """The exact failure: a stale reservation dating a brand-new browser."""
    j, flags, closed, mp = janitor
    now = time.time()
    mp.setattr(bj.tool_reservation, "last_activity", lambda r: now - DAYS)

    assert asyncio.run(j._tick(now)) is False
    assert closed == [], "a browser first seen this tick was closed as stale"
    assert flags[bj._FLAG] == now, "the idle window must start now"


def test_it_still_closes_a_browser_that_really_has_gone_idle(janitor):
    """The janitor's whole purpose has to survive the fix."""
    j, flags, closed, _ = janitor
    now = time.time()
    flags[bj._FLAG] = now - (4 * 3600)

    assert asyncio.run(j._tick(now)) is True
    assert closed == [True]


def test_a_browser_inside_its_window_is_left_alone(janitor):
    j, flags, closed, _ = janitor
    now = time.time()
    flags[bj._FLAG] = now - 3600

    assert asyncio.run(j._tick(now)) is False
    assert closed == []


def test_the_port_going_down_resets_the_window(janitor, monkeypatch):
    """So the next launch is judged from its own launch, not from history."""
    j, flags, closed, mp = janitor
    now = time.time()
    flags[bj._FLAG] = now - DAYS
    mp.setattr(bj, "_port_up", lambda *a, **k: False)

    assert asyncio.run(j._tick(now)) is False
    assert flags[bj._FLAG] == 0

    # Browser comes back: it must get a full window, not inherit the old one.
    mp.setattr(bj, "_port_up", lambda *a, **k: True)
    mp.setattr(bj.tool_reservation, "last_activity", lambda r: now - DAYS)
    assert asyncio.run(j._tick(now + 1)) is False
    assert closed == []


def test_a_reservation_still_counts_as_activity(janitor, monkeypatch):
    """The tool path must keep extending the window as it always did."""
    j, flags, closed, mp = janitor
    now = time.time()
    flags[bj._FLAG] = now - (4 * 3600)
    mp.setattr(bj.tool_reservation, "last_activity", lambda r: now - 60)

    assert asyncio.run(j._tick(now)) is False
    assert closed == []


def test_it_stands_down_entirely_when_launchd_supervises_the_browser(janitor, monkeypatch):
    """Otherwise launchd restarts what the janitor closes, every tick."""
    j, flags, closed, mp = janitor
    now = time.time()
    flags[bj._FLAG] = now - DAYS          # by every other rule, long overdue
    mp.setattr(bj, "_supervised", lambda: True)

    assert asyncio.run(j._tick(now)) is False
    assert closed == [], "a supervised browser must never be quit by the janitor"


def test_supervision_is_detected_from_the_installed_launchagent(monkeypatch, tmp_path):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(bj.Path, "home", staticmethod(lambda: tmp_path))
    assert bj._supervised() is False

    (agents / f"{bj._SUPERVISED_LABEL}.plist").write_text("<plist/>")
    assert bj._supervised() is True


def test_standing_down_is_logged_once_not_every_tick(janitor, monkeypatch, caplog):
    j, _, _, mp = janitor
    mp.setattr(bj, "_supervised", lambda: True)
    with caplog.at_level("INFO"):
        asyncio.run(j._tick(time.time()))
        asyncio.run(j._tick(time.time()))
    assert len([r for r in caplog.records if "standing down" in r.message]) == 1
