"""Embeddings: the half of memory that had never run.

Measured on the live store on 2026-08-22: 237 memories, 0 embeddings. Not
"embeddings computed and never used for recall" — never computed at all.

`_download_model` imported `optimum.onnxruntime`, which is not a dependency of
this project and pulls in torch. On every install without a pre-seeded model
directory the import raised ModuleNotFoundError, `store()` caught it, logged
one warning at WARNING level, and wrote the memory with a NULL vector.
`semantic_search` filters `WHERE embedding IS NOT NULL`, so it could only ever
return nothing, and the tool that offered it looked like it was working.

Three failures made that survivable for six weeks, and each has a test here:
the download path depended on a package nobody declared, the per-memory failure
was too quiet to notice, and nothing ever reported the resulting state.
"""

import asyncio
import logging

import pytest

from src.core.embedding import EmbeddingEngine

# Captured before the autouse no-download fixture replaces it.
_REAL_DOWNLOAD = EmbeddingEngine._download_model


def run(coro):
    return asyncio.run(coro)


def test_the_download_path_needs_no_package_outside_the_dependencies(tmp_path, monkeypatch):
    """The exact regression. The download is driven with a stubbed hub client,
    so this asserts the import graph and the file layout rather than the
    network: if the function reaches for optimum again, it raises here.
    """
    monkeypatch.setattr(EmbeddingEngine, "_download_model", _REAL_DOWNLOAD)

    fetched = []

    def fake_hub_download(repo, filename, **kwargs):
        fetched.append(filename)
        src = tmp_path / "hub" / filename
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"stub")
        return str(src)

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_hub_download)

    engine = EmbeddingEngine(model_dir=str(tmp_path / "model"))
    engine._download_model()

    assert "onnx/model.onnx" in fetched, "the exported graph was not fetched"
    assert "tokenizer.json" in fetched, "the tokenizer was not fetched"
    # The layout has to be one _find_onnx recognises, or the engine downloads
    # on every single call and still never loads.
    assert engine._find_onnx() is not None


def test_a_failure_is_recorded_once_and_not_retried_per_memory(tmp_path, monkeypatch):
    """Retrying a 130MB download once per stored memory turns a broken install
    into a slow one, and buries the reason under identical tracebacks.
    """
    calls = []

    def boom(self):
        calls.append(1)
        raise ModuleNotFoundError("No module named 'optimum'")

    monkeypatch.setattr(EmbeddingEngine, "_download_model", boom)
    engine = EmbeddingEngine(model_dir=str(tmp_path / "missing"))

    # The first call re-raises the real exception, so the traceback that
    # explains the install problem survives. Later calls raise the recorded
    # reason without touching the network again.
    with pytest.raises(ModuleNotFoundError):
        engine._ensure_loaded()
    for _ in range(2):
        with pytest.raises(RuntimeError):
            engine._ensure_loaded()

    assert len(calls) == 1, "the download was retried"
    assert engine.unavailable_reason
    assert "semantic search" in engine.unavailable_reason.lower()


def test_the_reason_names_the_consequence_not_just_the_exception(tmp_path, monkeypatch):
    """A message saying only "ModuleNotFoundError: optimum" is what let this
    run for six weeks. The line has to say what stopped working.
    """
    monkeypatch.setattr(EmbeddingEngine, "_download_model",
                        lambda self: (_ for _ in ()).throw(RuntimeError("nope")))
    engine = EmbeddingEngine(model_dir=str(tmp_path / "missing"))
    with pytest.raises(RuntimeError):
        engine._ensure_loaded()
    reason = engine.unavailable_reason
    assert "without vectors" in reason
    assert str(tmp_path / "missing") in reason, "the message must say where it looked"


def test_a_memory_stored_without_a_vector_is_counted(memory, monkeypatch):
    def boom(_self, _text):
        raise RuntimeError("model not installed")
    monkeypatch.setattr(type(memory.embedding), "embed_one", boom)

    async def go():
        for i in range(3):
            await memory.store(content=f"memory {i}", type="semantic", agent_id="t")

    run(go())
    assert memory.missing_embeddings() == 3
    assert memory._embed_failures == 3


def test_the_first_failure_is_logged_at_error_not_debug(memory, monkeypatch, caplog):
    """It was a warning, once, from inside a subprocess whose log nobody reads.
    Whatever level it is, it has to say that memories are being stored without
    vectors, because that is the part with a consequence.
    """
    def boom(_self, _text):
        raise RuntimeError("model not installed")
    monkeypatch.setattr(type(memory.embedding), "embed_one", boom)

    with caplog.at_level(logging.ERROR, logger="src.memory.sqlite"):
        run(memory.store(content="one", type="semantic", agent_id="t"))

    assert any("WITHOUT vectors" in r.message for r in caplog.records), caplog.text


def test_the_store_reports_missing_vectors_at_boot(tmp_path, fake_embeddings, caplog):
    """Said where it is read. A store whose vectors are missing looks healthy
    from every angle except the one query that needs them, so the count has to
    surface without anyone thinking to ask.
    """
    from src.memory.sqlite import SQLiteMemory

    store = SQLiteMemory(config={"path": str(tmp_path / "m.db")})
    run(store.store(content="a memory", type="semantic", agent_id="t"))
    store.db.execute("UPDATE memories SET embedding = NULL")
    store.db.commit()

    with caplog.at_level(logging.WARNING, logger="src.memory.sqlite"):
        SQLiteMemory(config={"path": str(tmp_path / "m.db")})

    assert any("no embedding" in r.message for r in caplog.records), caplog.text
    assert any("memory-backfill" in r.message for r in caplog.records), (
        "the warning must say how to fix it")


def test_a_memory_with_no_vector_is_invisible_to_semantic_search(memory, monkeypatch):
    """Why the NULL matters. This is not degraded ranking, it is absence: the
    query filters on `embedding IS NOT NULL`.
    """
    async def go():
        good = await memory.store(content="chrome debug port", type="semantic",
                                  agent_id="t", scope="global")
        memory.db.execute("UPDATE memories SET embedding = NULL WHERE id = ?", (good,))
        memory.db.commit()
        return await memory.semantic_search(query="chrome debug port", agent_id="t")

    assert run(go()) == []


def test_the_model_directory_follows_data_dir(tmp_path):
    """Same family as the store paths. The model is downloaded state, not code:
    left relative it landed under the repo while the memories it belongs to
    lived in the overlay.
    """
    from src.core.base import memory_config

    cfg = memory_config({"kbots": {"data_dir": str(tmp_path / "overlay-data")}})
    assert cfg["model_dir"].startswith(str(tmp_path / "overlay-data"))


def test_an_explicit_model_dir_still_wins(tmp_path):
    from src.core.base import memory_config

    cfg = memory_config({
        "kbots": {"data_dir": str(tmp_path / "overlay-data")},
        "defaults": {"memory": {"model_dir": "/opt/models/bge"}},
    })
    assert cfg["model_dir"] == "/opt/models/bge"
