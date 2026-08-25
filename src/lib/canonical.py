"""Canonical forms for graph entities and relations.

The extraction prompt has always asked the model to "reuse the exact same
entity spelling" and to pick "a short reusable snake_case relation". Measured
on the live fleet graph, that produced `Dr.Sable` beside `Dr. Sable`,
`blue-fox` beside `Blue Fox`, and 71 distinct relation types across 190
edges. Asking a model to be consistent is not a resolution strategy; this
module is, and it runs on every write regardless of who authored the edge.

Entity resolution here is deliberately deterministic: same name modulo case,
punctuation and whitespace means same entity. It will NOT merge `Ada`
with `Ada Lindqvist`, because a substring rule that merges those also
merges `Blue Fox` with `Blue Fox Shorts`, and a wrongly merged entity is
far harder to notice and undo than a duplicated one.
"""

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def entity_key(name: str) -> str:
    """The identity of an entity name: lowercase, alphanumerics only.

    `Dr. Sable`, `Dr.Sable` and `dr sable` share a key. `Ada` and
    `Ada Lindqvist` deliberately do not.
    """
    return _NON_ALNUM.sub("", (name or "").lower())


# The closed vocabulary. Chosen to cover what the fleet actually records:
# ownership, use, membership, authorship, location, people and publishing.
REL_VOCAB: tuple[str, ...] = (
    "uses",
    "part_of",
    "owns",
    "owned_by",
    "created",
    "created_by",
    "works_at",
    "works_on",
    "reports_to",
    "member_of",
    "located_in",
    "runs_on",
    "depends_on",
    "produces",
    "published_on",
    "distributed_via",
    "competes_with",
    "related_to",
    "instance_of",
    "has_role",
    "contacted_via",
    "scheduled_for",
    "blocked_by",
    "replaces",
    "prefers",
)

# Free-text relations the extractor and agents actually emitted, mapped onto
# the vocabulary. The point is not to be exhaustive: it is to collapse the
# obvious synonym tail so that traversal filtered by relation type works.
REL_SYNONYMS: dict[str, str] = {
    "use": "uses", "utilizes": "uses", "uses_tool": "uses", "using": "uses",
    "is_part_of": "part_of", "belongs_to": "part_of", "contains": "part_of",
    "subproject_of": "part_of", "component_of": "part_of",
    "own": "owns", "has": "owns", "possesses": "owns",
    "owns_stock": "owns", "owns_etf": "owns", "owns_fund": "owns",
    "is_owned_by": "owned_by",
    "authored": "created", "wrote": "created", "built": "created",
    "makes": "created", "made": "created", "creates": "created",
    "authored_by": "created_by", "written_by": "created_by",
    "built_by": "created_by", "made_by": "created_by",
    "employed_by": "works_at", "employee_of": "works_at",
    "founder_of": "works_at", "works_for": "works_at",
    "working_on": "works_on", "develops": "works_on", "maintains": "works_on",
    "manages": "works_on", "responsible_for": "works_on",
    "reports": "reports_to", "managed_by": "reports_to",
    "member": "member_of", "joined": "member_of",
    "in": "located_in", "located": "located_in", "lives_in": "located_in",
    "based_in": "located_in", "location": "located_in",
    "hosted_on": "runs_on", "deployed_on": "runs_on", "runs": "runs_on",
    "hosted_by": "runs_on", "served_by": "runs_on",
    "requires": "depends_on", "needs": "depends_on", "depends": "depends_on",
    "outputs": "produces", "generates": "produces", "offers": "produces",
    "provides": "produces",
    "published_at": "published_on", "publishes_on": "published_on",
    "posted_on": "published_on", "released_on": "published_on",
    "distributes_via": "distributed_via", "distributed_by": "distributed_via",
    "available_on": "distributed_via", "sold_on": "distributed_via",
    "competitor_of": "competes_with", "competes": "competes_with",
    "related": "related_to", "associated_with": "related_to",
    "connected_to": "related_to", "linked_to": "related_to",
    "is_a": "instance_of", "type_of": "instance_of", "kind_of": "instance_of",
    "role": "has_role", "acts_as": "has_role", "serves_as": "has_role",
    "contact": "contacted_via", "reachable_at": "contacted_via",
    "scheduled": "scheduled_for", "due": "scheduled_for",
    "blocks": "blocked_by", "blocked": "blocked_by",
    "supersedes": "replaces", "replaced_by": "replaces",
    "prefer": "prefers", "likes": "prefers", "favours": "prefers",
    "favors": "prefers",
}

# Relations where one subject has exactly one current object, so a new value
# supersedes the old one rather than sitting beside it. `uses` is not here: a
# project genuinely uses many tools. `works_at` is: a second employer almost
# always means the first one ended.
SINGLE_VALUED: frozenset[str] = frozenset({
    "works_at", "reports_to", "located_in", "runs_on", "owned_by", "has_role",
})

_REL_CLEAN = re.compile(r"[^a-z0-9]+")


def normalize_rel(rel: str) -> tuple[str, bool]:
    """Return (canonical relation, in_vocabulary).

    Out-of-vocabulary relations are normalised to snake_case and kept rather
    than coerced to `related_to`. Collapsing an unmatched relation to a generic
    one destroys meaning silently, which is the failure this module exists to
    stop; the caller logs the miss instead so vocabulary drift stays visible.
    """
    cleaned = _REL_CLEAN.sub("_", (rel or "").strip().lower()).strip("_")
    if not cleaned:
        return "related_to", True
    mapped = REL_SYNONYMS.get(cleaned, cleaned)
    return mapped, mapped in REL_VOCAB


def vocab_prompt_line() -> str:
    """The vocabulary as one line, for the extraction prompt.

    Generated from REL_VOCAB rather than written out again, so the prompt and
    the validator cannot drift apart the way the two intent lists in the
    Discord connector did.
    """
    return ", ".join(REL_VOCAB)
