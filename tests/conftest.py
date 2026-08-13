"""Shared pytest fixtures."""

import pytest

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
