"""Named-profile support + the 'login' (sign-in-once) flow of chrome_browser.

The concept being fenced in: each identity/service gets its own Chrome profile,
and sessions are established by the USER signing in by hand in a visible window
(action='login') — never by the agent handling credentials or copying cookies.
"""

import pytest

from src.core import session_consent, tool_reservation
from src.core.base import ToolContext
from src.tools import chrome_desktop

RES = "chrome_browser"


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


@pytest.fixture
def chrome(overlay, monkeypatch):
    """chrome_browser with consent pre-granted and no real Chrome in the picture."""
    monkeypatch.setattr(chrome_desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(chrome_desktop, "_port_up", lambda: False)
    for agent in ("atlas", "milo"):
        session_consent.grant(agent, chrome_desktop.CHROME_CAP)
    return chrome_desktop.chrome_browser


def _ctx(agent_id):
    return ToolContext(agent_id=agent_id, channel_id="c", user_id="u")


# --- profile name hygiene ---

@pytest.mark.parametrize("bad", ["../evil", "a/b", ".hidden", "x" * 33, "a\nb"])
async def test_bad_profile_names_are_rejected(chrome, bad):
    out = await chrome(_ctx("atlas"), "open", url="https://x.com",
                       profile=bad, auto_launch=False)
    assert "Invalid profile name" in out


async def test_bad_profile_name_reserves_nothing(chrome):
    await chrome(_ctx("atlas"), "open", url="https://x.com",
                 profile="../evil", auto_launch=False)
    assert tool_reservation.peek(RES) is None


async def test_good_profile_names_pass_validation(chrome):
    for good in ("flow", "Profile 1", "tiktok-a", "A_2"):
        out = await chrome(_ctx("atlas"), "open", url="https://x.com",
                           profile=good, auto_launch=False)
        assert "Invalid profile name" not in out
        await chrome(_ctx("atlas"), "release")


# --- the login action ---

async def test_login_requires_a_profile(chrome):
    out = await chrome(_ctx("atlas"), "login", url="https://accounts.x.com",
                       auto_launch=False)
    assert "'profile' required" in out
    assert tool_reservation.peek(RES) is None


async def test_login_requires_a_url(chrome):
    out = await chrome(_ctx("atlas"), "login", profile="flow", auto_launch=False)
    assert "'url' required" in out
    assert tool_reservation.peek(RES) is None


async def test_login_takes_the_reservation(chrome):
    out = await chrome(_ctx("atlas"), "login", url="https://accounts.x.com",
                       profile="flow", auto_launch=False)
    assert "chrome-debug.sh" in out          # reached the launch step, i.e. not blocked
    assert tool_reservation.peek(RES).agent_id == "atlas"


async def test_login_is_refused_while_another_agent_drives(chrome):
    await chrome(_ctx("atlas"), "open", url="https://x.com", auto_launch=False)
    out = await chrome(_ctx("milo"), "login", url="https://accounts.y.com",
                       profile="flow", auto_launch=False)
    assert "reserved by 'atlas'" in out


async def test_login_is_behind_the_consent_gate(overlay, monkeypatch):
    monkeypatch.setattr(chrome_desktop.platform, "system", lambda: "Darwin")
    out = await chrome_desktop.chrome_browser(
        _ctx("atlas"), "login", url="https://accounts.x.com",
        profile="flow", auto_launch=False)
    assert "PERMISSION NEEDED" in out
    assert tool_reservation.peek(RES) is None


# --- status reports profiles ---

async def test_status_lists_profiles_on_disk(chrome, tmp_path, monkeypatch):
    debug_dir = tmp_path / "chrome-debug"
    for name in ("Default", "flow"):
        (debug_dir / name).mkdir(parents=True)
        (debug_dir / name / "Preferences").write_text("{}")
    (debug_dir / "Cache").mkdir()            # no Preferences -> not a profile
    monkeypatch.setenv("KBOTS_CHROME_DEBUG_DIR", str(debug_dir))
    out = await chrome(_ctx("atlas"), "status")
    assert "Profiles: Default, flow" in out


async def test_status_without_debug_dir_stays_quiet(chrome, tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_CHROME_DEBUG_DIR", str(tmp_path / "nope"))
    out = await chrome(_ctx("atlas"), "status")
    assert "Profiles:" not in out


# --- driving a profile without a window ---

async def test_profile_actions_never_create_windows_implicitly(chrome, monkeypatch):
    """click/fill/etc. on an unpinned profile must refuse, not open a window."""
    monkeypatch.setattr(chrome_desktop, "_port_up", lambda: True)

    async def _fake_connect():
        class _Browser:
            def is_connected(self):
                return True
        chrome_desktop._state.update(browser=_Browser())
        return object()   # stands in for the default page

    monkeypatch.setattr(chrome_desktop, "_connect", _fake_connect)
    monkeypatch.setattr(chrome_desktop, "_ensure_chrome",
                        lambda auto_launch: _none())
    out = await chrome(_ctx("atlas"), "click", selector="button",
                       profile="flow", auto_launch=False)
    assert "No open window for profile 'flow'" in out
    chrome_desktop._state.clear()


async def _none():
    return None
