"""Local-models preflight — only when configured; runtime + model presence checks."""

import io
import json
import urllib.request

from src.core import preflight

ROUTER_CFG = {"defaults": {"llm": {
    "local": {"auto_pull": False},   # explicit pulls — missing models must warn
    "router": {"enabled": True, "router_model": "qwen3.5:2b",
               "local_model": "qwen3.5:9b"}}}}

AUTOPULL_CFG = {"defaults": {"llm": {"router": {
    "enabled": True, "router_model": "qwen3.5:2b", "local_model": "qwen3.5:9b"}}}}


def test_not_configured_is_silent():
    assert preflight._local_models_in_use({"defaults": {"llm": {}}}) == []
    assert preflight._check_local_models({"defaults": {"llm": {}}}) == []


def test_models_collected_from_router_and_agents():
    cfg = {"defaults": {"llm": {"local": {"model": "qwen3.5:9b"},
                                "router": {"enabled": True, "router_model": "qwen3.5:2b",
                                           "local_model": "qwen3.5:9b"}}},
           "agents": {"a": {"llm": {"provider": "local", "model": "gemma4:26b-moe"}}}}
    assert preflight._local_models_in_use(cfg) == ["gemma4:26b-moe", "qwen3.5:2b", "qwen3.5:9b"]


def test_runtime_down_warns(monkeypatch):
    def boom(url, timeout=0):
        raise OSError("refused")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    warns = preflight._check_local_models(ROUTER_CFG)
    assert len(warns) == 1 and "no runtime is reachable" in warns[0]


def test_missing_model_warns_with_pull_command(monkeypatch):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    def fake_open(url, timeout=0):
        return _Resp(json.dumps({"data": [{"id": "qwen3.5:2b"}]}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    warns = preflight._check_local_models(ROUTER_CFG)
    assert warns == ["local model 'qwen3.5:9b' is configured but not downloaded — "
                     "run: ollama pull qwen3.5:9b"]


def test_all_present_is_clean(monkeypatch):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    def fake_open(url, timeout=0):
        return _Resp(json.dumps({"data": [{"id": "qwen3.5:2b"},
                                          {"id": "qwen3.5:9b"}]}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    assert preflight._check_local_models(ROUTER_CFG) == []


def test_missing_model_silent_when_autopull(monkeypatch):
    """auto_pull (default on) makes missing models informational, not a warning."""
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    def fake_open(url, timeout=0):
        return _Resp(json.dumps({"data": [{"id": "qwen3.5:2b"}]}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    assert preflight._check_local_models(AUTOPULL_CFG) == []
