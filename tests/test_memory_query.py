"""Query building and rank fusion — the two things recall is made of.

Regression (2026-08-22): auto-recall passed the user's raw sentence into an
FTS5 MATCH. Replayed against 300 real user messages from the live store, 71%
raised a syntax error and fell through to `LIKE '%whole message%'`, which can
only match a memory containing the entire message verbatim, so it matched
nothing. Only 4% of messages returned any memory at all, which is why 237
stored memories were invisible unless an agent called a memory tool by hand.

These tests are mostly about the punctuation in ordinary questions, because
that is what broke it: a question mark, a hyphen or an apostrophe.
"""

import pytest

from src.memory.query import STOPWORDS, fts_query, rrf_merge

# Verbatim shapes from the live corpus. Each one used to raise
# sqlite3.OperationalError as an FTS5 expression.
REAL_MESSAGES = [
    "when i react to messages with thumbs-up i get no response from agents... does anything happen?",
    "What channels does kbots need? Aproval? Update? Alert?",
    "ok but when main agents discord bot gets invited to guild/server for the first time",
    "I ran the setup for kbots on another machine and it gave me this error:",
    "Why don't we do a overhaul on the whole memory logic",
    "fix it so no PR is open and everything is merged with no memory drop",
    "chrome won't stay up on :9222 -- what's wrong",
    "someone.surname@example.com",
    'he said "deploy it" and then left',
]


@pytest.mark.parametrize("message", REAL_MESSAGES)
def test_real_user_messages_produce_a_valid_fts_expression(message, memory):
    """Every one of these used to be a syntax error. None may be now."""
    import asyncio
    expr = fts_query(message)
    assert expr, f"nothing searchable extracted from {message!r}"
    # The proof is not that the string looks right, it is that FTS5 accepts it.
    asyncio.run(memory.store(content="a memory about chrome and deploy", type="semantic",
                             agent_id="t", scope="global"))
    memory.db.execute(
        "SELECT m.id FROM memories_fts fts JOIN memories m ON m.rowid = fts.rowid "
        "WHERE memories_fts MATCH ?", [expr]).fetchall()


def test_terms_are_ored_not_anded():
    """FTS5 ANDs by default, which is why long questions matched nothing.

    A memory that answers two words of a ten-word question is a useful hit.
    Requiring all ten is the difference between recall and silence.
    """
    expr = fts_query("chrome debug port stays down")
    assert " OR " in expr
    assert " AND " not in expr


def test_stopwords_are_dropped_but_real_terms_survive():
    expr = fts_query("what is the status of the deploy")
    assert "deploy" in expr and "status" in expr
    assert '"the"' not in expr and '"is"' not in expr


def test_every_term_is_quoted_so_fts_keywords_cannot_be_interpreted():
    """`NEAR`, `OR` and a bare column filter are operators unless quoted."""
    expr = fts_query("NEAR content: something")
    assert '"NEAR"' in expr
    assert ":" not in expr.replace('"', "")


def test_short_tokens_are_dropped_but_numbers_are_kept():
    expr = fts_query("port 9222 is a b c down")
    assert '"9222"' in expr
    assert '"a"' not in expr and '"b"' not in expr


def test_no_searchable_content_returns_none_not_a_match_everything():
    """The caller must be able to tell "nothing to search for" from "no hits".

    Returning an empty expression would MATCH nothing anyway, but returning
    None makes the distinction explicit at the one place a future change might
    be tempted to substitute a wildcard.
    """
    assert fts_query("") is None
    assert fts_query("??? !!!") is None
    assert fts_query("a the of") is None
    # Bare FTS5 operators and nothing else. This is the input most likely to
    # produce a syntactically valid expression that means "everything".
    assert fts_query("AND OR NOT") is None


def test_term_count_is_capped():
    expr = fts_query(" ".join(f"term{i}" for i in range(50)), max_terms=12)
    assert expr.count(" OR ") == 11


def test_duplicate_terms_appear_once():
    expr = fts_query("deploy deploy DEPLOY the deploy")
    assert expr.count("deploy") + expr.count("DEPLOY") == 1


def test_stoplist_holds_no_word_that_carries_retrieval_signal():
    """A stoplist that removes a real query term is worse than no stoplist.

    Pinned because the tempting next edit is to grow this list, and words like
    'chrome', 'memory', 'agent' or 'no' are exactly the ones a fleet-specific
    stoplist would swallow. 'no' is in the list of words people reach for; it
    is not in ours.
    """
    for word in ("chrome", "memory", "agent", "deploy", "graph", "vault", "port"):
        assert word not in STOPWORDS


# --- rank fusion ---

def _hits(*ids):
    return [{"id": i, "content": i} for i in ids]


def test_agreement_between_engines_outranks_one_engines_top_hit():
    """The whole point of fusing: two weak agreeing votes beat one strong one.

    `b` is nobody's first choice and both engines found it. `a` is one engine's
    first choice and the other never saw it.
    """
    merged = rrf_merge([("keyword", _hits("a", "b")), ("semantic", _hits("c", "b"))])
    assert merged[0]["id"] == "b"


def test_each_result_names_the_engines_that_found_it():
    merged = rrf_merge([("keyword", _hits("a")), ("semantic", _hits("a", "b"))])
    by_id = {m["id"]: m for m in merged}
    assert by_id["a"]["sources"] == ["keyword", "semantic"]
    assert by_id["b"]["sources"] == ["semantic"]


def test_scores_from_different_engines_are_never_compared():
    """FTS5 rank is negative-is-better; cosine similarity is 0..1.

    Fusion must use position only. If it ever started reading a score field,
    this ordering would inverleave differently: here the semantic list carries
    a huge similarity on its second item and must still not overtake the
    keyword winner they both agree on.
    """
    keyword = [{"id": "x", "rank": -9.5}, {"id": "y", "rank": -0.1}]
    semantic = [{"id": "x", "similarity": 0.01}, {"id": "y", "similarity": 0.99}]
    merged = rrf_merge([("keyword", keyword), ("semantic", semantic)])
    assert [m["id"] for m in merged] == ["x", "y"]


def test_an_empty_engine_does_not_break_or_dominate_fusion():
    merged = rrf_merge([("keyword", []), ("semantic", _hits("a")), ("graph", None)])
    assert [m["id"] for m in merged] == ["a"]
    assert merged[0]["sources"] == ["semantic"]


def test_results_without_an_id_are_skipped_rather_than_merged_together():
    """Two id-less rows are not the same row, and must not fuse into one."""
    merged = rrf_merge([("keyword", [{"content": "one"}, {"content": "two"}])])
    assert merged == []


def test_fusion_is_stable_for_identical_input():
    args = [("keyword", _hits("a", "b", "c")), ("semantic", _hits("c", "a", "b"))]
    assert [m["id"] for m in rrf_merge(args)] == [m["id"] for m in rrf_merge(args)]
