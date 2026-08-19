"""Tests for src/core/startup_context.py — codex index resolution and injection."""

from pathlib import Path

import pytest

from src.core import startup_context
from src.core.agent_scaffold import scaffold_agent


@pytest.fixture
def no_core_codex(tmp_path, monkeypatch):
    """Point the core-codex fallback at an empty dir so only fixtures count."""
    monkeypatch.setattr(startup_context, "PROJECT_ROOT", tmp_path / "engine")
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    return tmp_path


def _write_index(root: Path, text: str) -> Path:
    index = root / "codex" / "_index.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(text)
    return index


def test_no_codex_anywhere(no_core_codex):
    assert startup_context._build_codex_index() is None


def test_shared_codex_from_overlay(no_core_codex, monkeypatch):
    overlay = no_core_codex / "overlay"
    _write_index(overlay, "# Shared knowledge")
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))

    block = startup_context._build_codex_index()
    assert block is not None
    assert "<codex-index>" in block
    assert "# Shared knowledge" in block
    assert "<agent-codex-index>" not in block


def test_shared_codex_falls_back_to_core(no_core_codex):
    _write_index(no_core_codex / "engine", "# Core sample")

    block = startup_context._build_codex_index()
    assert block is not None
    assert "# Core sample" in block


def test_agent_codex_only(no_core_codex):
    agent_dir = no_core_codex / "agents" / "researcher"
    _write_index(agent_dir, "# Researcher's own docs")

    block = startup_context._build_codex_index(str(agent_dir))
    assert block is not None
    assert "<agent-codex-index>" in block
    assert "# Researcher's own docs" in block
    assert "<codex-index>" not in block


def test_shared_and_agent_codex_both_injected(no_core_codex, monkeypatch):
    overlay = no_core_codex / "overlay"
    _write_index(overlay, "# Shared knowledge")
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    agent_dir = no_core_codex / "agents" / "researcher"
    _write_index(agent_dir, "# Researcher's own docs")

    block = startup_context._build_codex_index(str(agent_dir))
    assert block is not None
    assert "# Shared knowledge" in block
    assert "# Researcher's own docs" in block
    # Shared knowledge comes first, agent-specific second
    assert block.index("<codex-index>") < block.index("<agent-codex-index>")


def test_agent_codex_block_names_its_path(no_core_codex):
    agent_dir = no_core_codex / "agents" / "researcher"
    _write_index(agent_dir, "# Docs")

    block = startup_context._build_codex_index(str(agent_dir))
    assert str(agent_dir / "codex") in block


def test_scaffold_creates_agent_codex(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "agents").mkdir()

    written = scaffold_agent(
        tmp_path, "research", "Research Bot", "Finds things out",
        engine_root=tmp_path / "engine",
    )

    index = tmp_path / "agents" / "research" / "codex" / "_index.md"
    assert index in written
    assert index.exists()
    assert "Research Bot" in index.read_text()


def test_scaffold_keeps_existing_agent_codex(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "agents").mkdir()
    index = _write_index(tmp_path / "agents" / "research", "# Hand-written index")

    written = scaffold_agent(
        tmp_path, "research", "Research Bot", "Finds things out",
        engine_root=tmp_path / "engine",
    )

    assert index not in written
    assert index.read_text() == "# Hand-written index"
