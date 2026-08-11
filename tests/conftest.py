"""Shared pytest fixtures."""

import pytest

from src.llm.mock import MockProvider


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
