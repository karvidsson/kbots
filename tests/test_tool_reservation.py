"""Cross-process reservations + the chrome_browser turn-taking gate.

The bug being fenced off: two agents attach to the same real Chrome, one
navigates the tab out from under the other, and the second reads the wrong page
without any exception to warn it.
"""

import json
import os

import pytest

from src.core import session_consent, tool_reservation
from src.core.base import ToolContext
from src.tools import chrome_desktop

RES = "chrome_browser"
TTL = tool_reservation.RESERVATION_TTL


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


# --- the store ---

def test_clean_acquire(overlay):
    ok, res = tool_reservation.acquire(RES, "atlas", now=1000)
    assert ok is True
    assert res.agent_id == "atlas"
    assert res.pid == os.getpid()
    assert res.acquired_at == res.last_used_at == 1000


def test_record_lives_in_the_shared_overlay_not_tmp(overlay):
    tool_reservation.acquire(RES, "atlas", now=1000)
    path = overlay / "data" / "reservations" / "chrome_browser.json"
    assert path.exists(), "reservation must land in the overlay all agents share"
    assert json.loads(path.read_text())["agent_id"] == "atlas"


def test_contention_second_agent_is_refused(overlay):
    tool_reservation.acquire(RES, "atlas", now=1000)
    ok, holder = tool_reservation.acquire(RES, "milo", now=1130)
    assert ok is False
    assert holder.agent_id == "atlas"
    # ...and the holder's record is untouched by the failed attempt
    assert tool_reservation.peek(RES, now=1130).agent_id == "atlas"


def test_busy_message_names_holder_and_timing(overlay):
    tool_reservation.acquire(RES, "milo", now=1000)
    _, holder = tool_reservation.acquire(RES, "atlas", now=1000 + 130)
    msg = tool_reservation.busy_message(holder, "Use the `browser` tool.", now=1130)
    assert "milo" in msg
    assert "idle 2m10s" in msg
    assert "expires in 7m50s" in msg
    assert "`browser`" in msg


def test_idle_expiry_at_ten_minutes(overlay):
    assert TTL == 600, "the agreed idle window is 10 minutes"
    tool_reservation.acquire(RES, "atlas", now=1000)

    ok, holder = tool_reservation.acquire(RES, "milo", now=1000 + TTL - 1)
    assert ok is False and holder.agent_id == "atlas"      # one second short

    ok, res = tool_reservation.acquire(RES, "milo", now=1000 + TTL)
    assert ok is True and res.agent_id == "milo"          # expired → taken over


def test_ttl_is_env_overridable(monkeypatch):
    monkeypatch.setenv("KBOTS_TOOL_RESERVATION_TTL", "42")
    import importlib
    reloaded = importlib.reload(tool_reservation)
    try:
        assert reloaded.RESERVATION_TTL == 42
    finally:
        monkeypatch.delenv("KBOTS_TOOL_RESERVATION_TTL")
        importlib.reload(tool_reservation)


def test_every_call_refreshes_so_an_active_session_never_expires(overlay):
    tool_reservation.acquire(RES, "atlas", now=1000)
    for t in range(1500, 4000, 500):          # busy agent, well past one TTL total
        ok, res = tool_reservation.acquire(RES, "atlas", now=t)
        assert ok is True
        assert res.acquired_at == 1000        # original claim preserved
        assert res.last_used_at == t          # heartbeat moves

    ok, holder = tool_reservation.acquire(RES, "milo", now=4000)
    assert ok is False and holder.agent_id == "atlas"


def test_takeover_when_holder_process_is_dead(overlay, monkeypatch):
    tool_reservation.acquire(RES, "atlas", pid=999999, now=1000)
    monkeypatch.setattr(tool_reservation, "_pid_alive", lambda pid: pid != 999999)

    ok, res = tool_reservation.acquire(RES, "milo", now=1010)   # far inside the TTL
    assert ok is True and res.agent_id == "milo"


def test_live_holder_process_blocks_takeover(overlay):
    tool_reservation.acquire(RES, "atlas", pid=os.getpid(), now=1000)
    ok, _ = tool_reservation.acquire(RES, "milo", now=1010)
    assert ok is False


def test_same_agent_reentrancy_does_not_deadlock(overlay):
    for _ in range(5):
        ok, _ = tool_reservation.acquire(RES, "atlas", now=1000)
        assert ok is True


def test_reentrancy_adopts_a_new_process_for_the_same_agent(overlay):
    """One agent can be re-invoked in a fresh MCP process — still its own turn."""
    tool_reservation.acquire(RES, "atlas", pid=1234, now=1000)
    ok, res = tool_reservation.acquire(RES, "atlas", pid=5678, now=1100)
    assert ok is True
    assert res.pid == 5678
    assert res.acquired_at == 1000


def test_release_frees_it(overlay):
    tool_reservation.acquire(RES, "atlas", now=1000)
    assert tool_reservation.release(RES, "atlas") is True
    assert tool_reservation.peek(RES, now=1000) is None
    ok, _ = tool_reservation.acquire(RES, "milo", now=1001)
    assert ok is True


def test_release_by_a_non_holder_is_a_no_op(overlay):
    tool_reservation.acquire(RES, "atlas", now=1000)
    assert tool_reservation.release(RES, "milo") is False
    assert tool_reservation.peek(RES, now=1000).agent_id == "atlas"


def test_peek_reports_free_when_expired(overlay):
    tool_reservation.acquire(RES, "atlas", now=1000)
    assert tool_reservation.peek(RES, now=1000).agent_id == "atlas"
    assert tool_reservation.peek(RES, now=1000 + TTL) is None


def test_corrupt_record_does_not_wedge_the_tool(overlay):
    tool_reservation.acquire(RES, "atlas", now=1000)
    (overlay / "data" / "reservations" / "chrome_browser.json").write_text("{not json")
    ok, res = tool_reservation.acquire(RES, "milo", now=1000)
    assert ok is True and res.agent_id == "milo"


def test_resources_are_independent(overlay):
    tool_reservation.acquire("chrome_browser", "atlas", now=1000)
    ok, _ = tool_reservation.acquire("some_other_tool", "milo", now=1000)
    assert ok is True


def test_concurrent_acquire_has_exactly_one_winner(overlay):
    """The race the lock exists for: read-then-write would let both agents in."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda i: tool_reservation.acquire(RES, f"agent{i}", pid=os.getpid())[0],
            range(8)))
    assert sum(results) == 1


# --- the chrome_browser gate ---

@pytest.fixture
def chrome(overlay, monkeypatch):
    """chrome_browser with consent pre-granted and no real Chrome in the picture."""
    monkeypatch.setattr(chrome_desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(chrome_desktop, "_port_up", lambda port: False)
    for agent in ("atlas", "milo"):
        session_consent.grant(agent, chrome_desktop.CHROME_CAP)
    return chrome_desktop.chrome_browser


def _ctx(agent_id):
    return ToolContext(agent_id=agent_id, channel_id="c", user_id="u")


async def test_gate_first_agent_gets_through(chrome):
    out = await chrome(_ctx("atlas"), "open", url="https://x.com", auto_launch=False)
    assert "reserved by" not in out
    assert "chrome-debug.sh" in out          # reached the launch step, i.e. not blocked
    assert tool_reservation.peek(RES).agent_id == "atlas"


async def test_gate_second_agent_is_refused_with_a_useful_message(chrome):
    await chrome(_ctx("atlas"), "open", url="https://x.com", auto_launch=False)
    out = await chrome(_ctx("milo"), "open", url="https://y.com", auto_launch=False)
    assert "reserved by 'atlas'" in out
    assert "expires in" in out
    assert "`browser`" in out                # told where to go instead
    assert "chrome-debug.sh" not in out      # refused before launching anything


async def test_gate_refuses_before_launching_chrome(chrome, monkeypatch):
    """A blocked agent must not start a browser as a side effect."""
    launched = []

    async def _spy(inst, auto_launch):
        launched.append(auto_launch)
        return "stubbed — no Chrome started"

    monkeypatch.setattr(chrome_desktop, "_ensure_chrome", _spy)
    await chrome(_ctx("atlas"), "open", url="https://x.com", auto_launch=False)
    launched.clear()
    await chrome(_ctx("milo"), "open", url="https://y.com", auto_launch=True)
    assert launched == []


async def test_gate_is_reentrant_for_the_same_agent(chrome):
    ctx = _ctx("atlas")
    for _ in range(3):
        out = await chrome(ctx, "open", url="https://x.com", auto_launch=False)
        assert "reserved by" not in out


async def test_release_action_hands_the_tool_over(chrome):
    await chrome(_ctx("atlas"), "open", url="https://x.com", auto_launch=False)
    out = await chrome(_ctx("atlas"), "release")
    assert "Released" in out
    out = await chrome(_ctx("milo"), "open", url="https://y.com", auto_launch=False)
    assert "reserved by" not in out


async def test_close_also_releases(chrome):
    await chrome(_ctx("atlas"), "open", url="https://x.com", auto_launch=False)
    await chrome(_ctx("atlas"), "close")
    assert tool_reservation.peek(RES) is None


async def test_status_stays_answerable_while_another_agent_drives(chrome):
    await chrome(_ctx("atlas"), "open", url="https://x.com", auto_launch=False)
    out = await chrome(_ctx("milo"), "status")
    assert "Reserved by 'atlas'" in out
    assert tool_reservation.peek(RES).agent_id == "atlas"   # status took nothing


async def test_status_does_not_reserve(chrome):
    await chrome(_ctx("atlas"), "status")
    assert tool_reservation.peek(RES) is None


async def test_consent_gate_still_runs_before_the_reservation(overlay, monkeypatch):
    """An agent without consent must be told about consent, and reserve nothing."""
    monkeypatch.setattr(chrome_desktop.platform, "system", lambda: "Darwin")
    out = await chrome_desktop.chrome_browser(
        _ctx("atlas"), "open", url="https://x.com", auto_launch=False)
    assert "PERMISSION NEEDED" in out
    assert tool_reservation.peek(RES) is None
