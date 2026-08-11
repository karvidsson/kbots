"""Tests for persistent agent overrides (Storage.get/set_agent_override)
plus the resolution order used by AgentManager: override > agents.yaml > unset.
"""

import tempfile
from pathlib import Path

import pytest

from src.core.storage import Storage


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as tmp:
        s = Storage(db_path=str(Path(tmp) / "test.db"))
        await s.init()
        try:
            yield s
        finally:
            await s.close()


@pytest.mark.asyncio
async def test_get_missing_override_returns_none(storage):
    assert await storage.get_agent_override("talkie", "effort") is None


@pytest.mark.asyncio
async def test_set_and_get_override(storage):
    await storage.set_agent_override("talkie", "effort", "high")
    assert await storage.get_agent_override("talkie", "effort") == "high"


@pytest.mark.asyncio
async def test_upsert_replaces_value(storage):
    await storage.set_agent_override("talkie", "effort", "low")
    await storage.set_agent_override("talkie", "effort", "medium")
    assert await storage.get_agent_override("talkie", "effort") == "medium"


@pytest.mark.asyncio
async def test_clear_override_with_none(storage):
    await storage.set_agent_override("talkie", "effort", "high")
    await storage.set_agent_override("talkie", "effort", None)
    assert await storage.get_agent_override("talkie", "effort") is None


@pytest.mark.asyncio
async def test_multiple_settings_per_agent(storage):
    await storage.set_agent_override("talkie", "model", "sonnet")
    await storage.set_agent_override("talkie", "effort", "high")
    overrides = await storage.get_agent_overrides("talkie")
    assert overrides == {"model": "sonnet", "effort": "high"}


@pytest.mark.asyncio
async def test_isolation_across_agents(storage):
    await storage.set_agent_override("talkie", "effort", "medium")
    await storage.set_agent_override("kai", "effort", "low")
    assert await storage.get_agent_override("talkie", "effort") == "medium"
    assert await storage.get_agent_override("kai", "effort") == "low"


def _resolve(overrides: dict, yaml_val, setting: str):
    """Mirror the resolution order used in agent_manager.py."""
    return overrides.get(setting, yaml_val)


def test_resolution_order_override_wins():
    assert _resolve({"effort": "high"}, "low", "effort") == "high"


def test_resolution_order_yaml_fallback():
    assert _resolve({}, "medium", "effort") == "medium"


def test_resolution_order_unset_propagates_none():
    assert _resolve({}, None, "effort") is None
