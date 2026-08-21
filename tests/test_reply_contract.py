"""Every agent gets the reply contract, in every session, unconditionally.

Concision already existed as guidance: codex rule 9, "Short, punchy,
actionable". All eight agents carried it and all eight ignored it, because it
sat as one line inside a long index block competing with the roster and the
version banner, and because it asked for a tone rather than a shape.

The contract replaces the adjective with a structure and a number. These tests
pin the parts that make it load-bearing: that it reaches every agent whatever
else is missing, that the decision shape is present in full, and that the word
budget is a figure rather than a description.
"""

import asyncio

import pytest

from src.core import startup_context as sc


def _context(**kwargs):
    return asyncio.run(sc.build_startup_context(kwargs.pop("agent_id", "any-agent"), **kwargs))


def test_the_contract_is_injected_for_any_agent():
    assert "<reply-contract>" in _context()


def test_it_survives_having_no_roster_no_codex_and_no_memory(monkeypatch):
    """An agent stripped of every other block still gets the contract.

    build_startup_context used to return None when nothing was available, so
    the contract must be appended outside that check.
    """
    monkeypatch.setattr(sc, "_build_platform_version", lambda: None)
    monkeypatch.setattr(sc, "_build_lessons", lambda p: None)
    monkeypatch.setattr(sc, "_build_team_summary", lambda: None)
    monkeypatch.setattr(sc, "_build_codex_index", lambda p: None)

    ctx = _context()
    assert ctx is not None, "the contract must never be dropped"
    assert "<reply-contract>" in ctx


def test_it_comes_last_so_it_is_read_closest_to_the_request():
    ctx = _context()
    assert ctx.rindex("<reply-contract>") > ctx.rindex("<codex-index>")


@pytest.mark.parametrize("marker", [
    "DECISION:", "OPTIONS:", "I'D PICK:", "IF NO REPLY:",
])
def test_the_decision_shape_is_given_in_full(marker):
    """A partial template gets filled in with prose, which is the failure mode."""
    assert marker in _context()


def test_the_word_budget_is_a_number_not_an_adjective():
    ctx = _context()
    assert str(sc.DECISION_WORD_BUDGET) in ctx
    assert isinstance(sc.DECISION_WORD_BUDGET, int)


def test_no_reply_is_named_as_the_default_for_routine_work():
    ctx = _context()
    assert "NO_REPLY" in ctx
    assert "default" in ctx.lower()


def test_the_contract_itself_stays_short():
    """A contract that costs more attention than it saves is self-defeating."""
    body = _context().split("<reply-contract>")[1].split("</reply-contract>")[0]
    assert len(body.split()) < 350, f"contract is {len(body.split())} words"
