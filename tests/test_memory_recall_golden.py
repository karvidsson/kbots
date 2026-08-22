"""A golden query set: does recall actually find the right memory?

Every other memory test asserts that a component behaves. This one asserts that
the system answers questions, which is the only claim anybody cares about and
the one nothing measured before. It is the reason the pipeline was made fixed
rather than routed: with one path through recall, a number here describes the
system instead of describing how a particular question happened to be worded.

The corpus and the questions are deliberately written in different words from
each other, the way a person asks about something they half-remember. A test
whose question repeats the memory's own vocabulary measures nothing: keyword
search alone passes it.

The floor is a floor, not a target. Raise it when recall improves; never lower
it to make a change pass, because that is the failure it exists to catch.
"""

import asyncio

import pytest

from src.memory.recall import recall

# (content, entities it mentions) — entities stand in for what the reflector
# extracts, so the graph hop has something to walk.
CORPUS = [
    ("The supervised debug Chrome listens on port 9222 and is kept alive by launchd.",
     ["Chrome", "launchd"]),
    ("BrowserJanitor closes an idle debug browser after three hours.",
     ["BrowserJanitor", "Chrome"]),
    ("scripts/self-deploy.sh pulls, syncs dependencies, runs the gate and restarts.",
     ["self-deploy.sh"]),
    ("The gate is ruff plus the full pytest suite; a red gate rolls back.",
     ["self-deploy.sh", "pytest"]),
    ("Ada dislikes em dashes in any prose, including commit messages.",
     ["Ada"]),
    ("Reports to Ada lead with the outcome and keep the evidence below it.",
     ["Ada"]),
    ("The encrypted vault must never be copied; two backups reached GitHub that way.",
     ["vault", "GitHub"]),
    ("Blue Fox publishes tracks on Bandpost and short videos on Shortform.",
     ["Blue Fox", "Bandpost", "Shortform"]),
    ("Tally reconciles the bank CSV export every month and flags anomalies.",
     ["Tally"]),
    ("Free-tier Supabase pauses after seven idle days and drops out of DNS.",
     ["Supabase", "example.tech"]),
    ("The papers page on example.tech is served by a Cloudflare Worker.",
     ["example.tech", "Cloudflare"]),
    ("Agent home channels are Discord DMs, which need their own gateway intent.",
     ["Discord"]),
]

# (question, the fragment of the memory that answers it). Phrased as a person
# would ask, not as the memory is written.
GOLDEN = [
    ("which port is the browser on?", "9222"),
    ("why does chrome keep dying?", "idle debug browser"),
    ("what happens when I ship new engine code?", "self-deploy.sh"),
    ("what runs before a restart?", "ruff"),
    ("what writing habit should I avoid in a report?", "em dashes"),
    ("where does the music go?", "Bandpost"),
    ("who handles the invoices?", "bank CSV"),
    ("why did the papers page break?", "Supabase"),
    ("what serves example.tech?", "Cloudflare Worker"),
    ("why did my thumbs-up do nothing?", "gateway intent"),
]

# Two floors, because there are two things worth measuring and only one of them
# can run offline.
#
# LEXICAL_FLOOR is what the pipeline achieves with keyword search and the graph
# hop alone, which is the configuration these tests run in: the real sentence
# encoder is a 130MB download and no test may fetch it. Three of ten is not
# good, and it is not meant to be — it is the guard that says fusion and the
# graph hop still work at all.
#
# SEMANTIC_FLOOR is the real number, measured with the actual model, and the
# test that checks it skips when the model is not on disk. Both exist because
# the interesting failure is a change that quietly disables one engine: with a
# single blended floor, losing the vector engine and gaining a little keyword
# luck look identical.
LEXICAL_FLOOR = 3
SEMANTIC_FLOOR = 8


class _Graph:
    """A graph over the corpus entities: co-mention is an edge."""

    def __init__(self, corpus):
        self.edges = []
        for _, entities in corpus:
            for i, a in enumerate(entities):
                for b in entities[i + 1:]:
                    self.edges.append({"src": a, "rel": "related_to", "dst": b})

    async def related(self, entity, depth=1, agent_id=None, limit=50):
        return [e for e in self.edges if entity in (e["src"], e["dst"])][:limit]


def _seed(store):
    async def go():
        for content, entities in CORPUS:
            mid = await store.store(content=content, type="semantic",
                                    agent_id="eng", scope="global")
            await store.anchor_entities(mid, entities)
    asyncio.run(go())
    return store, _Graph(CORPUS)


@pytest.fixture
def corpus(memory):
    """The corpus with stubbed embeddings: keyword and graph only."""
    return _seed(memory)


@pytest.fixture
def real_corpus(tmp_path):
    """The corpus with the real sentence encoder, or a skip.

    Deliberately does not use the `memory` fixture, which stubs embeddings, and
    deliberately does not let the engine download: a test that fetches 130MB is
    a test that fails on a fresh checkout and in CI for a reason unrelated to
    the code.
    """
    from src.core.embedding import EmbeddingEngine
    from src.memory.sqlite import SQLiteMemory

    probe = EmbeddingEngine(model_dir=str(_model_dir()))
    if probe._find_onnx() is None:
        pytest.skip(f"embedding model not installed at {_model_dir()}")
    return _seed(SQLiteMemory(config={"path": str(tmp_path / "m.db"),
                                      "model_dir": str(_model_dir())}))


def _model_dir():
    from src.core.base import PROJECT_ROOT
    return PROJECT_ROOT / "data" / "models" / "bge-small-en-v1.5"


def _answers(memory, graph, question, k=3):
    results = asyncio.run(recall(memory, question, agent_id="eng", limit=k, graph=graph))
    return [r.get("content", "") for r in results]


def _score(memory, graph):
    hits, misses = 0, []
    for question, expected in GOLDEN:
        if any(expected in c for c in _answers(memory, graph, question)):
            hits += 1
        else:
            misses.append(question)
    return hits, misses


def test_recall_answers_the_golden_set_without_a_vector_engine(corpus):
    """Keyword plus the graph hop, with the encoder stubbed out."""
    hits, misses = _score(*corpus)
    assert hits >= LEXICAL_FLOOR, (
        f"recall answered {hits}/{len(GOLDEN)}, lexical floor is {LEXICAL_FLOOR}. "
        f"Missed: {misses}")


def test_recall_answers_the_golden_set_with_the_real_model(real_corpus):
    """The number that describes the deployed system.

    Skipped where the model is not installed, which until this was fixed was
    everywhere: `_download_model` imported `optimum`, which is not a dependency
    of this project, so every install stored memories with NULL vectors and
    semantic search returned nothing. That is why this floor had never been
    measured before.
    """
    hits, misses = _score(*real_corpus)
    assert hits >= SEMANTIC_FLOOR, (
        f"recall answered {hits}/{len(GOLDEN)}, floor is {SEMANTIC_FLOOR}. "
        f"Missed: {misses}")


def test_the_old_single_keyword_path_scores_worse_than_the_fused_one(corpus):
    """The comparison that justifies the whole change.

    Not a fixed number for the old path: the point is the direction. If a
    future change ever makes plain keyword search match fused recall on this
    set, the fusion is buying nothing and should be reconsidered rather than
    kept out of habit.
    """
    memory, graph = corpus

    def keyword_only(question):
        return [r.get("content", "") for r in
                asyncio.run(memory.search(query=question, agent_id="eng", limit=3))]

    fused = sum(1 for q, e in GOLDEN if any(e in c for c in _answers(memory, graph, q)))
    keyword = sum(1 for q, e in GOLDEN if any(e in c for c in keyword_only(q)))
    assert fused > keyword, f"fused {fused} did not beat keyword-only {keyword}"


def test_every_golden_question_returns_something(corpus):
    """Separate from correctness on purpose. Returning the wrong memory is a
    ranking problem; returning nothing is the bug that was actually live, and
    it has a different cause and a different fix.
    """
    memory, graph = corpus
    empty = [q for q, _ in GOLDEN if not _answers(memory, graph, q)]
    assert empty == [], f"these questions recalled nothing at all: {empty}"


def test_the_golden_answers_are_all_actually_in_the_corpus():
    """Guards the harness itself: an expectation that matches no memory would
    make the floor unreachable for a reason that has nothing to do with recall.
    """
    blob = " ".join(c for c, _ in CORPUS)
    for _, expected in GOLDEN:
        assert expected in blob, f"{expected!r} is in no memory"


def test_the_questions_do_not_simply_quote_their_answers():
    """The harness is only meaningful if the questions are worded differently
    from the memories. Sharing one content word is normal; sharing the answer
    fragment itself would make this a keyword test wearing a costume.
    """
    for question, expected in GOLDEN:
        assert expected.lower() not in question.lower()
