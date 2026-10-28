"""The send loop guard budgets agent pairs, not hops."""

from src.core.agent_mail_budget import PairBudget


def test_pair_budget_counts_both_directions_and_frees_up_after_the_window():
    b = PairBudget(limit=3, window=100, repeat_window=10)
    assert b.admit("a", "b", "1", now=0) == ""
    assert b.admit("b", "a", "2", now=1) == ""
    assert b.admit("a", "b", "3", now=2) == ""
    refused = b.admit("b", "a", "4", now=3)
    assert "exchanged 3 messages" in refused and "98s" in refused
    assert b.admit("b", "a", "4", now=101) == ""  # the first one aged out


def test_other_pairs_are_unaffected():
    b = PairBudget(limit=1, window=100)
    assert b.admit("a", "b", "x", now=0) == ""
    assert b.admit("a", "c", "x", now=0) == ""
    assert b.admit("a", "b", "y", now=1) != ""


def test_repeat_is_whitespace_and_case_blind_and_expires():
    b = PairBudget(limit=10, window=100, repeat_window=10)
    assert b.admit("a", "b", "Done.  See file", now=0) == ""
    assert "exact message" in b.admit("a", "b", "done. see file", now=5)
    assert b.admit("a", "b", "done. see file", now=11) == ""
    # The same words to someone else, or back the other way, are not repeats.
    assert b.admit("a", "c", "done. see file", now=12) == ""
    assert b.admit("b", "a", "done. see file", now=12) == ""


def test_a_refused_message_does_not_spend_budget():
    b = PairBudget(limit=2, window=100, repeat_window=50)
    assert b.admit("a", "b", "x", now=0) == ""
    assert b.admit("a", "b", "x", now=1) != ""   # repeat refused
    assert b.admit("a", "b", "y", now=2) == ""   # still had room
