"""Query building and rank fusion for memory recall.

Two things live here, both of which used to be missing entirely.

`fts_query` exists because auto-recall passed the user's raw sentence straight
into an FTS5 MATCH. A sentence is not an FTS5 query: `?`, `-`, `'` and bare
`AND`/`OR`/`NOT` are operators or syntax errors, and even when a sentence
parses, FTS5 ANDs every token, so recall needs the memory to contain all of
them. Measured against the real store, 71% of user messages raised a syntax
error and fell through to `LIKE '%whole message%'` (which matches nothing), and
only 4% of messages returned any memory at all.

`rrf_merge` exists because there is more than one way to find a memory and no
good reason to make the model choose between them. Reciprocal rank fusion
combines ranked lists without needing their scores to be comparable, which
matters here: FTS5 `rank` and cosine similarity are on unrelated scales.
"""

import re

# Deliberately short. A stoplist that removes real query terms is worse than
# none, so this covers only words that carry no retrieval signal at all.
STOPWORDS = frozenset("""
a about all also am an and any are as at be been but by can could did do does
doing done for from get got had has have he her here hers him his how i if in
into is it its just let me my no nor not now of on once only or other our out
over own same she should so some such than that the their them then there these
they this those through to too under until up us very was we were what when
where which while who whom why will with would you your
""".split())

# FTS5 bareword operators: unquoted, these change the query's meaning or break
# it. Every term is quoted on the way out, so this is belt and braces.
_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def fts_query(text: str, max_terms: int = 12) -> str | None:
    """Build a safe FTS5 MATCH expression from arbitrary user text.

    Terms are OR-ed rather than AND-ed: a memory matching two of a sentence's
    words is a useful hit, and requiring all of them is what made recall return
    nothing. FTS5 ranks by relevance, so more matched terms still sorts higher.

    Returns None when nothing searchable survives, which the caller must treat
    as "no keyword results", never as "search everything".
    """
    if not text:
        return None
    terms: list[str] = []
    seen: set[str] = set()
    for m in _TOKEN.finditer(text):
        t = m.group(0)
        low = t.lower()
        if low in seen or low in STOPWORDS:
            continue
        # Single characters and bare digits are noise; a token like "9222" is
        # not, so keep numbers of two or more digits.
        if len(t) < 2:
            continue
        seen.add(low)
        terms.append(t)
        if len(terms) >= max_terms:
            break
    if not terms:
        return None
    # Double quotes make each term a literal string, so an FTS5 keyword such as
    # NEAR or a term containing a column filter cannot be interpreted.
    return " OR ".join(f'"{t}"' for t in terms)


def rrf_merge(ranked_lists, k: int = 60, key=lambda r: r.get("id")) -> list[dict]:
    """Reciprocal rank fusion over several ranked result lists.

    score(d) = sum over lists of 1 / (k + rank(d)), rank starting at 1.

    k damps the top of each list so that one engine's confident first result
    cannot alone outrank a document that several engines agree on. 60 is the
    value from the original RRF paper and is not tuned here.

    Each result keeps a `sources` list naming which engines found it, because
    "three engines agree" is worth showing to whatever reads the bundle.
    """
    scores: dict = {}
    merged: dict = {}
    for name, results in ranked_lists:
        for rank, item in enumerate(results or [], start=1):
            ident = key(item)
            if ident is None:
                continue
            scores[ident] = scores.get(ident, 0.0) + 1.0 / (k + rank)
            if ident not in merged:
                merged[ident] = dict(item)
                merged[ident]["sources"] = []
            if name not in merged[ident]["sources"]:
                merged[ident]["sources"].append(name)
    out = list(merged.values())
    for item in out:
        item["fusion_score"] = round(scores[key(item)], 6)
    out.sort(key=lambda r: r["fusion_score"], reverse=True)
    return out
