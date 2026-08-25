"""UX regression tests for the setup wizard's input handling."""

import builtins
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "setup_ux", Path(__file__).resolve().parent.parent / "setup.py")
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)

GUILD = "1000000000000000001"
CHAN = "1000000000000000002"


@pytest.fixture
def feed(monkeypatch):
    """Feed a queue of keystrokes to input()."""
    def _feed(*answers):
        it = iter(answers)
        monkeypatch.setattr(builtins, "input", lambda *a, **k: next(it))
    return _feed


def test_snowflake_validation():
    assert setup._is_snowflake(GUILD)
    assert not setup._is_snowflake("+1")
    assert not setup._is_snowflake("abc")
    assert not setup._is_snowflake("123")          # too short
    assert not setup._is_snowflake("")


def test_ask_id_accepts_valid(feed):
    feed(GUILD)
    assert setup.ask_id("Server ID") == GUILD


def test_ask_id_reprompts_then_accepts(feed):
    # bad → "try again? y" → good
    feed("not-an-id", "y", GUILD)
    assert setup.ask_id("Server ID") == GUILD


def test_ask_id_blank_skips_when_optional(feed):
    feed("")
    assert setup.ask_id("Server ID") == ""


def test_ask_ids_dedupes_and_validates(feed):
    # same id 3x (the real bug), one bad, one new, then blank to finish
    feed(GUILD, GUILD, GUILD, "garbage", CHAN, "")
    assert setup.ask_ids("server") == [GUILD, CHAN]


def test_ask_menu_returns_index(feed):
    feed("2")
    assert setup.ask_menu("Pick", ["a", "b", "c"], default=1) == 2
    feed("")  # blank → default
    assert setup.ask_menu("Pick", ["a", "b", "c"], default=1) == 1


def test_routing_all_channels_simplest(feed):
    # mentions? y | menu 1 (all channels) | restrict users? n
    feed("y", "1", "n")
    r = setup._ask_routing("main")["discord"]
    assert r == {"account": "main", "channels": [], "mentions": True}


def test_routing_specific_channels_with_dupes(feed):
    # mentions? y | menu 2 | channel CHAN, dup, blank | restrict users? n
    feed("y", "2", CHAN, CHAN, "", "n")
    r = setup._ask_routing("main")["discord"]
    assert r["channels"] == [CHAN]            # deduped
    assert r["mentions"] is True
    assert "users" not in r                   # declined → key omitted


def test_routing_never_asks_guild_restriction(feed):
    """The per-agent server allowlist is a hand-edit knob, not a wizard
    question — single-server installs would only ever answer 'no', and a
    guild default would silently break later invites."""
    feed("y", "1", "n")
    r = setup._ask_routing("main")["discord"]
    assert "guilds" not in r


USER = "1000000000000000003"


def test_routing_user_allowlist(feed):
    # mentions? y | menu 1 | restrict users? y | USER, blank
    feed("y", "1", "y", USER, "")
    r = setup._ask_routing("main")["discord"]
    assert r["users"] == [USER]


# --- Step 12c: training-data collection ---

def test_step_training_defaults_off(feed):
    feed("")                                # Enter → default No
    state = {}
    setup.step_training(state)
    assert state["training_collection"] == {"enabled": False, "include_tool_trace": True}


def test_step_training_enable(feed):
    feed("y")
    state = {}
    setup.step_training(state)
    assert state["training_collection"] == {"enabled": True, "include_tool_trace": True}
