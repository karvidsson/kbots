"""Session-scoped consent store + the chrome_browser permission gate."""

import pytest

from src.core import session_consent
from src.core.base import ToolContext
from src.tools import chrome_desktop


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


# --- store ---

def test_grant_and_expiry(overlay):
    assert session_consent.is_granted("atlas", "chrome_browser", now=1000) is False
    session_consent.grant("atlas", "chrome_browser", ttl=100, now=1000)
    assert session_consent.is_granted("atlas", "chrome_browser", now=1050) is True
    assert session_consent.is_granted("atlas", "chrome_browser", now=1101) is False  # expired


def test_revoke(overlay):
    session_consent.grant("atlas", "chrome_browser", ttl=1000, now=1000)
    assert session_consent.is_granted("atlas", "chrome_browser", now=1000) is True
    session_consent.revoke("atlas", "chrome_browser")
    assert session_consent.is_granted("atlas", "chrome_browser", now=1000) is False


def test_scoped_per_agent(overlay):
    session_consent.grant("atlas", "chrome_browser", now=1000)
    assert session_consent.is_granted("atlas", "chrome_browser", now=1000) is True
    assert session_consent.is_granted("ledger", "chrome_browser", now=1000) is False  # not shared


# --- the chrome_browser gate (macOS-guarded, so run the gate logic directly) ---

async def test_gate_blocks_without_consent(overlay, monkeypatch):
    monkeypatch.setattr(chrome_desktop.platform, "system", lambda: "Darwin")
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="u")
    out = await chrome_desktop.chrome_browser(ctx, "open", url="https://x.com", auto_launch=False)
    assert "PERMISSION NEEDED" in out


async def test_grant_then_unlock_then_revoke(overlay, monkeypatch):
    monkeypatch.setattr(chrome_desktop.platform, "system", lambda: "Darwin")
    # Simulate "no debug Chrome" deterministically — otherwise a debug Chrome
    # actually running on the port (e.g. from a live session) changes the message.
    monkeypatch.setattr(chrome_desktop, "_port_up", lambda: False)
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="u")

    assert "granted" in await chrome_desktop.chrome_browser(ctx, "grant")
    # gate now passes → with auto_launch=False it reaches the no-Chrome hint, NOT the wall
    out = await chrome_desktop.chrome_browser(ctx, "open", url="https://x.com", auto_launch=False)
    assert "PERMISSION NEEDED" not in out
    assert "chrome-debug.sh" in out

    assert "revoked" in await chrome_desktop.chrome_browser(ctx, "revoke")
    out = await chrome_desktop.chrome_browser(ctx, "open", url="https://x.com", auto_launch=False)
    assert "PERMISSION NEEDED" in out


async def test_status_is_free_action(overlay, monkeypatch):
    monkeypatch.setattr(chrome_desktop.platform, "system", lambda: "Darwin")
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="u")
    out = await chrome_desktop.chrome_browser(ctx, "status")   # no consent needed
    assert "PERMISSION NEEDED" not in out
