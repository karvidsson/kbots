"""Tests for the bot install links the wizard prints after storing a token."""

import importlib.util
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_spec = importlib.util.spec_from_file_location(
    "setup_invite", Path(__file__).resolve().parent.parent / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)

APP_ID = "1000000000000000009"


def _params(url: str) -> dict:
    return parse_qs(urlparse(url).query)


def test_invite_url_shape():
    p = _params(setup.invite_url(APP_ID))
    assert p["client_id"] == [APP_ID]
    assert p["scope"] == ["bot applications.commands"]


def test_invite_url_never_grants_administrator():
    for manage in (False, True):
        perms = int(_params(setup.invite_url(APP_ID, manage))["permissions"][0])
        assert not perms & (1 << 3)   # ADMINISTRATOR
        assert not perms & (1 << 5)   # MANAGE_GUILD


def test_manage_channels_only_for_setup_account():
    base = int(_params(setup.invite_url(APP_ID, manage_channels=False))["permissions"][0])
    main = int(_params(setup.invite_url(APP_ID, manage_channels=True))["permissions"][0])
    assert not base & (1 << 4)
    assert main & (1 << 4)
    assert main == base | (1 << 4)


def test_invite_url_covers_documented_baseline():
    perms = int(_params(setup.invite_url(APP_ID))["permissions"][0])
    for bit in (1 << 6,    # Add Reactions
                1 << 10,   # View Channels
                1 << 11,   # Send Messages
                1 << 14,   # Embed Links
                1 << 15,   # Attach Files
                1 << 16,   # Read Message History
                1 << 26):  # Change Nickname
        assert perms & bit, bin(bit)


def test_application_id_falls_back_to_bot_user(monkeypatch):
    monkeypatch.setattr(
        setup.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")))
    assert setup._application_id("tok", {"id": APP_ID}) == APP_ID
    assert setup._application_id("tok", None) == ""


def test_show_invite_link_records_for_summary(monkeypatch, capsys):
    monkeypatch.setattr(setup, "_application_id", lambda t, b: APP_ID)
    state: dict = {}
    setup.show_invite_link(state, "main", "tok", {"id": APP_ID}, manage_channels=True)
    assert state["invite_urls"]["main"] == setup.invite_url(APP_ID, manage_channels=True)
    assert APP_ID in capsys.readouterr().out


def test_show_invite_link_silent_offline(monkeypatch, capsys):
    monkeypatch.setattr(setup, "_application_id", lambda t, b: "")
    state: dict = {}
    setup.show_invite_link(state, "main", "tok", None)
    assert "invite_urls" not in state
    assert "authorize" not in capsys.readouterr().out
