"""Entity keys and the closed relation vocabulary.

Measured on the live fleet graph before this landed: 167 entities holding 190
edges, 110 of them with degree 0 or 1, and 71 distinct relation names. `uses`
and `part_of` carried a third of the edges and the rest were mostly singletons
invented one extraction pass at a time. `Dr.Sable` sat beside `Dr. Sable`,
`blue-fox` beside `Blue Fox`.

The extraction prompt had always asked the model to "reuse the exact same
entity spelling" and to pick "a short reusable snake_case relation". Asking a
model to be consistent is not a resolution strategy. These tests pin the one
that is.
"""

import pytest

from src.lib.canonical import (
    REL_SYNONYMS,
    REL_VOCAB,
    SINGLE_VALUED,
    entity_key,
    normalize_rel,
    vocab_prompt_line,
)


@pytest.mark.parametrize("a,b", [
    ("Dr.Sable", "Dr. Sable"),
    ("blue-fox", "Blue Fox"),
    ("Ridge Runner", "ridge-runner"),
    ("self-deploy.sh", "self deploy sh"),
    ("KBOTS", "kbots"),
    ("house-move-app", "House Move App"),
])
def test_spellings_of_the_same_thing_share_a_key(a, b):
    """Every one of these pairs was two separate nodes on the live graph."""
    assert entity_key(a) == entity_key(b)


@pytest.mark.parametrize("a,b", [
    ("Ada", "Ada Lindqvist"),
    ("Blue Fox", "Blue Fox Shorts"),
    ("kbots", "kbots-overlay"),
])
def test_substrings_are_deliberately_not_merged(a, b):
    """A rule that merges 'Ada' into 'Ada Lindqvist' also merges
    'Blue Fox' into 'Blue Fox Shorts'. A wrongly merged entity is far
    harder to notice and undo than a duplicated one, so this stays conservative
    and the duplicate survives.
    """
    assert entity_key(a) != entity_key(b)


def test_a_name_with_no_alphanumerics_has_no_key():
    """Callers use the empty key to reject a name rather than storing junk."""
    assert entity_key("???") == ""
    assert entity_key("") == ""
    assert entity_key(None) == ""


@pytest.mark.parametrize("raw,expected", [
    ("utilizes", "uses"),
    ("Uses", "uses"),
    ("is_part_of", "part_of"),
    ("works for", "works_at"),
    ("employed_by", "works_at"),
    ("hosted-on", "runs_on"),
    ("WORKS ON", "works_on"),
])
def test_synonyms_collapse_onto_the_vocabulary(raw, expected):
    rel, known = normalize_rel(raw)
    assert (rel, known) == (expected, True)


def test_an_unknown_relation_keeps_its_meaning_and_is_reported():
    """Coercing an unmatched relation to `related_to` would destroy meaning
    silently, which is the failure this module exists to stop. It is kept,
    normalised, and flagged so vocabulary drift stays visible in the logs.
    """
    rel, known = normalize_rel("sponsors the tour of")
    assert rel == "sponsors_the_tour_of"
    assert known is False


def test_an_empty_relation_becomes_the_generic_one():
    assert normalize_rel("") == ("related_to", True)
    assert normalize_rel("   ") == ("related_to", True)


def test_normalisation_strips_markup_so_a_relation_cannot_carry_a_payload():
    rel, _ = normalize_rel("</script><img src=x onerror=alert(1)>")
    assert "<" not in rel and ">" not in rel and "(" not in rel


def test_normalisation_is_idempotent():
    """Applied on write and again on read; the second pass must be a no-op."""
    for raw in list(REL_SYNONYMS) + list(REL_VOCAB) + ["totally made up thing"]:
        once = normalize_rel(raw)[0]
        assert normalize_rel(once)[0] == once


def test_every_synonym_maps_into_the_vocabulary():
    """A synonym pointing at a non-vocabulary target is a silent dead end: it
    would normalise, report itself as known, and still not be traversable by
    relation type.
    """
    for source, target in REL_SYNONYMS.items():
        assert target in REL_VOCAB, f"{source!r} maps to {target!r}, not in the vocabulary"


def test_no_synonym_shadows_a_vocabulary_term():
    for term in REL_VOCAB:
        assert REL_SYNONYMS.get(term) in (None, term)


def test_single_valued_relations_are_all_real_relations():
    assert SINGLE_VALUED <= set(REL_VOCAB)


def test_uses_is_not_single_valued():
    """A project genuinely uses many tools. Marking `uses` single-valued would
    make every new tool silently retire the previous one, which is the most
    destructive possible reading of a correct fact.
    """
    assert "uses" not in SINGLE_VALUED
    assert "part_of" not in SINGLE_VALUED
    assert "works_at" in SINGLE_VALUED


def test_the_prompt_line_is_generated_from_the_vocabulary():
    """The extraction prompt and the validator must not drift apart.

    This is the same shape as the Discord intents bug: two hand-maintained
    copies of one list, one of which was updated. Here the prompt is generated,
    so a term added to REL_VOCAB reaches the model automatically.
    """
    line = vocab_prompt_line()
    for term in REL_VOCAB:
        assert term in line


def test_the_extraction_prompt_actually_carries_the_vocabulary():
    from src.core.reflector import _EXTRACT_SYSTEM
    assert vocab_prompt_line() in _EXTRACT_SYSTEM
