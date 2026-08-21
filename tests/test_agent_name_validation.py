"""An agent name is checked where it is typed, not eight steps later.

Regression (2026-08-20, reported from a fresh install on a second machine):
setup.py asked for the agent name with a bare ask() and never checked it. The
name was first validated inside scaffold_agent during step_generate, so

    Setup failed: Invalid agent name 'Botson' — lowercase letters, digits, ...

arrived after every other question had been answered, aborted the wizard and
rolled the install back. One capital letter cost the whole run.
"""

import re

import pytest

from src.core.agent_scaffold import (
    AGENT_NAME_RULE,
    agent_name_error,
    scaffold_agent,
    suggest_agent_name,
)


@pytest.mark.parametrize("name", ["main", "engineer", "a", "bot-2", "neon_husky",
                                  "x" * 32])
def test_valid_names_are_accepted(name):
    assert agent_name_error(name) is None


@pytest.mark.parametrize("name,expect", [
    ("Botson", "capital"),        # the reported case
    ("MAIN", "capital"),
    ("2fast", "start"),
    ("-lead", "start"),
    ("my bot", "not allowed"),
    ("bot!", "not allowed"),
    ("x" * 33, "33 characters"),
    ("", "empty"),
])
def test_invalid_names_say_what_is_actually_wrong(name, expect):
    """A generic rule restatement makes the user guess which rule they broke."""
    problem = agent_name_error(name)
    assert problem is not None
    assert expect in problem, problem


def test_the_capital_letter_case_points_at_the_display_name():
    """'Botson' is a display name typed into the internal-name field.

    Naming the other field is what stops the user fixing it and hitting the
    same wall on the next prompt.
    """
    problem = agent_name_error("Botson")
    assert "display name" in problem.lower()


@pytest.mark.parametrize("raw,expected", [
    ("Botson", "botson"),
    ("MAIN", "main"),
    ("My Bot 2", "my-bot-2"),
    ("  Neon Husky  ", "neon-husky"),
    ("2fast", "fast"),
    ("bot!", "bot"),
])
def test_a_usable_name_is_suggested_from_what_was_typed(raw, expected):
    assert suggest_agent_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "!!!", "123", "---"])
def test_unsalvageable_input_yields_no_suggestion(raw):
    """Empty means 'ask again', not 'accept this'."""
    assert suggest_agent_name(raw) == ""


def test_every_suggestion_is_itself_valid():
    for raw in ["Botson", "My Bot 2", "UPPER_case", "a b c", "x" * 60]:
        s = suggest_agent_name(raw)
        if s:
            assert agent_name_error(s) is None, f"{raw!r} -> {s!r}"


def test_scaffold_agent_uses_the_same_check(tmp_path):
    """One rule, not two that can drift apart."""
    (tmp_path / "config").mkdir(parents=True)
    with pytest.raises(ValueError) as exc:
        scaffold_agent(overlay=tmp_path, name="Botson", display_name="Botson",
                       description="test", tier="assistant")
    assert "capital" in str(exc.value)


def test_the_rule_is_stated_once_and_reused():
    """setup.py prints AGENT_NAME_RULE; it must not hand-roll its own wording."""
    assert "lowercase" in AGENT_NAME_RULE and "32" in AGENT_NAME_RULE
    setup_src = (__import__("pathlib").Path(__file__).parent.parent / "setup.py").read_text()
    assert "AGENT_NAME_RULE" in setup_src


def test_setup_validates_at_the_prompt_not_at_generate():
    """Both name prompts must go through the validating helper.

    A bare ask() here is the bug: it defers the failure past the point where a
    correction is cheap.
    """
    setup_src = (__import__("pathlib").Path(__file__).parent.parent / "setup.py").read_text()
    bare = re.findall(r'agent_name = ask\((?!_)', setup_src)
    assert bare == [], f"{len(bare)} agent-name prompt(s) still unvalidated"
    assert setup_src.count("ask_agent_name(") >= 3   # definition + both prompts
