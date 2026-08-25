"""One name question per agent — the Discord-facing name the user actually
wants drives the internal name (folder, config key, bot account), instead of
the wizard asking for the same identity three times."""

import builtins
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "setup_one_name", Path(__file__).resolve().parent.parent / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


@pytest.fixture
def feed(monkeypatch):
    def _feed(*answers):
        it = iter(answers)
        monkeypatch.setattr(builtins, "input", lambda *a, **k: next(it))
    return _feed


def test_display_name_drives_internal_name(feed):
    feed("Atlas")
    assert setup.ask_display_name("Agent name", "Main") == ("Atlas", "atlas")


def test_spaces_and_caps_slugify(feed):
    feed("My Bot 2")
    assert setup.ask_display_name("Agent name", "Main") == ("My Bot 2", "my-bot-2")


def test_default_is_used_on_enter(feed):
    feed("")
    assert setup.ask_display_name("Agent name", "Engineer") == ("Engineer", "engineer")


def test_underivable_name_reprompts(feed):
    # all digits → nothing usable survives → ask again
    feed("2000", "Atlas")
    assert setup.ask_display_name("Agent name", "Main") == ("Atlas", "atlas")


def test_step_discord_is_the_only_agent_step():
    """The merged step owns the whole identity — a separate step_agent would
    reintroduce the duplicate name questions this removed."""
    assert not hasattr(setup, "step_agent")


class _Vault:
    def __init__(self):
        self.stored = {}

    def get(self, k):
        return self.stored.get(k)

    def set(self, k, v):
        self.stored[k] = v


def test_merged_step_builds_agent_and_bot_from_one_name(feed, monkeypatch):
    monkeypatch.setattr(setup, "validate_discord_token",
                        lambda t: ({"username": "AppName", "id": "1" * 17}, ""))
    monkeypatch.setattr(setup, "show_invite_link", lambda *a, **k: None)
    guild, owner = "1000000000000000001", "1000000000000000003"
    # name | description | model | personality | token(hidden) | guild | owner
    # | routing: mentions y, scope 1, users n
    monkeypatch.setattr(setup.getpass, "getpass", lambda *a, **k: "tok")
    feed("Atlas", "the butler", "2", "", guild, owner, "y", "1", "n")

    state = {"vault": _Vault()}
    setup.step_discord(state)

    assert state["agent"]["name"] == "atlas"
    assert state["agent"]["display_name"] == "Atlas"
    assert state["agent"]["model"] == "opus"
    assert state["agent"]["routing"]["discord"]["account"] == "atlas"
    assert state["bot_name"] == "atlas"
    assert state["vault"].stored["discord-token"] == "tok"
    assert state["guild_id"] == guild
    assert state["discord_skip"] is False
