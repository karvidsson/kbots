"""Shared pytest fixtures."""

import pytest

from src.core.base import PROJECT_ROOT
from src.llm.mock import MockProvider


@pytest.fixture(autouse=True)
def _isolate_roster(tmp_path, monkeypatch):
    """No test may write to a real team.json.

    TEAM_FILE resolves through KBOTS_OVERLAY, so on a machine with a live
    overlay — which is exactly how scripts/self-deploy.sh runs the suite — the
    roster tools wrote fixture agents straight into the production roster.
    reconcile_roster rebuilds from config on the next restart and pruned them,
    which is why it went unnoticed; between a test run and a restart the real
    roster carried invented agents.

    Autouse rather than opt-in on purpose: every test that forgets is the one
    that does the damage. Tests needing their own roster still monkeypatch
    TEAM_FILE themselves, which simply overrides this.
    """
    roster = tmp_path / "_isolated_roster" / "team.json"
    roster.parent.mkdir(parents=True, exist_ok=True)
    roster.write_text('{"humans": [], "agents": []}')

    import importlib
    for mod_name in ("src.tools.team", "src.core.startup_context"):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:            # module may not exist in a trimmed build
            continue
        if hasattr(mod, "TEAM_FILE"):
            monkeypatch.setattr(mod, "TEAM_FILE", roster)
    return roster


@pytest.fixture(autouse=True)
def _isolate_runtime_flags(tmp_path, monkeypatch):
    """No test may read this deployment's live runtime flags.

    Same failure as `_isolate_roster` above, one file over. runtime.json holds
    what an admin has flipped live (the HITL killswitch, the alert channel,
    reply shortening), it resolves through KBOTS_OVERLAY, and a runtime flag
    deliberately BEATS the config a caller passes in. So on a machine where the
    feature is switched on, a test that constructs its subject with an explicit
    config is silently overridden by production state.

    It cost a deploy. Reply shortening was turned on fleet-wide at a 1200-char
    threshold; the reply-shorten tests build a shortener at 300 and feed it a
    ~500-char reply, got the live 1200 instead, and eight of them failed. The
    gate in self-deploy.sh runs the suite against the live overlay, so it went
    red and rolled back a release that was green in CI and green in a dev
    checkout. Every machine that had used the feature would have failed the
    same way, and only those machines.

    Pointing the module's own path resolvers at a tmp file, rather than moving
    KBOTS_OVERLAY, so tests that legitimately exercise overlay paths are
    unaffected. `_read_path` returns the file whether or not it exists:
    `get_flag` already treats an unreadable file as "no flags set".
    """
    from src.core import runtime_state

    flags = tmp_path / "_isolated_runtime" / "runtime.json"
    flags.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runtime_state, "_path", lambda: flags)
    monkeypatch.setattr(runtime_state, "_read_path", lambda: flags)
    return flags


@pytest.fixture(autouse=True)
def _isolate_inter_agent_depth(monkeypatch):
    """No test may inherit this process's inter-agent call depth.

    KBOTS_INTER_AGENT_DEPTH is threaded into agent subprocesses through the
    environment, so running the suite from inside an agent turn that was itself
    started by another agent leaves it set — and the loopback tests read it,
    asserted a depth of 1, and got 2. The suite then failed for a reason that
    had nothing to do with the code under test and could not be reproduced from
    a plain shell or in CI.

    Autouse, and paired with _isolate_roster above, for the same reason: the
    test that forgets is the one that gets it wrong.
    """
    monkeypatch.delenv("KBOTS_INTER_AGENT_DEPTH", raising=False)


@pytest.fixture
def overlay(tmp_path):
    """A minimal overlay directory structure (config/ + agents/)."""
    (tmp_path / "config").mkdir()
    (tmp_path / "agents").mkdir()
    return tmp_path


@pytest.fixture
def mock_llm():
    """An offline mock LLM provider (echoes the last user message)."""
    return MockProvider(config={})


@pytest.fixture(autouse=True)
def _no_writes_into_core(request):
    """No test may leave a file in the Core checkout.

    Nothing an installation produces belongs in Core, and the test suite is an
    installation like any other. Four stray skill YAMLs were sitting untracked
    in skills/ when this was added, written by tests that expressed "somewhere
    disposable" by pointing PROJECT_ROOT at a tmpdir. That works until the code
    resolves its write path some other way, and then the tests quietly start
    writing into the repo instead of failing.

    Autouse and checked after the fact rather than prevented, because the point
    is to catch the write path nobody thought of. Named files only: a full tree
    scan would fight __pycache__ and .venv for no gain.
    """
    watched = [PROJECT_ROOT / "skills", PROJECT_ROOT / "tools",
               PROJECT_ROOT / "codex", PROJECT_ROOT / "config"]
    before = {d: set(d.glob("*")) for d in watched if d.is_dir()}
    yield
    for d, existing in before.items():
        new = set(d.glob("*")) - existing
        if new:
            for f in new:                      # leave the checkout as we found it
                f.unlink() if f.is_file() else None
            pytest.fail(
                f"{request.node.name} wrote into the Core checkout: "
                f"{sorted(str(f.relative_to(PROJECT_ROOT)) for f in new)}. "
                f"Point KBOTS_OVERLAY at tmp_path instead.")


@pytest.fixture(autouse=True)
def _no_model_downloads(monkeypatch):
    """No test may fetch the 130MB embedding model.

    Autouse because the download is triggered from deep inside `store()`, four
    frames below any test that happens to write a memory through a real
    SQLiteMemory. It was invisible for as long as the download path was broken
    (it raised ModuleNotFoundError immediately); the moment that was fixed, a
    plain `pytest` on a fresh checkout started pulling 130MB over the network
    before the first assertion.

    Tests that want real embeddings use the model already on disk and skip when
    it is absent — see tests/test_memory_recall_golden.py.
    """
    from src.core.embedding import EmbeddingEngine

    def refuse(self):
        raise RuntimeError(
            "the embedding model is not installed and tests must not download it")

    monkeypatch.setattr(EmbeddingEngine, "_download_model", refuse)


@pytest.fixture
def fake_embeddings(monkeypatch):
    """Deterministic offline embeddings, so vector search is testable.

    The real engine lazily downloads a 130MB ONNX model on first use, which no
    test may do. Stubbing embed_one to return None-equivalents would be
    simpler, but then semantic_search silently degrades to keyword search and
    every fusion test would be measuring one engine while claiming to measure
    two.

    This is a hashed bag of words: texts sharing vocabulary score high, texts
    sharing none score zero. Crude next to a real sentence encoder, and exactly
    enough to assert that the vector engine ran, contributed, and ranked.
    """
    import zlib

    import numpy as np

    from src.core.embedding import DIMENSIONS, EmbeddingEngine

    def embed_one(self, text: str):
        # crc32, not hash(): str hashing is salted per process, so a failure
        # would reproduce only under the PYTHONHASHSEED that produced it.
        vec = np.zeros(DIMENSIONS, dtype=np.float32)
        for token in str(text).lower().split():
            token = token.strip(".,:;!?()[]\"'")
            if token:
                vec[zlib.crc32(token.encode()) % DIMENSIONS] += 1.0
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm else vec

    monkeypatch.setattr(EmbeddingEngine, "embed_one", embed_one)
    return embed_one


@pytest.fixture
def memory(tmp_path, fake_embeddings):
    """A SQLiteMemory on a throwaway database, with offline embeddings."""
    from src.memory.sqlite import SQLiteMemory
    return SQLiteMemory(config={"path": str(tmp_path / "memory.db")})
