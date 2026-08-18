"""smart_browser (Stagehand extra) — everything testable without the SDK.

The `stagehand` package is an optional dependency and absent in CI; these
tests cover the pure helpers (backend resolution, callback message shaping,
schema handling) and the tool's validation paths that never reach the SDK.
"""

import json
import types

import pytest
import stagehand_browser as sb

from src.core.base import ToolContext


def _ctx(vault=None):
    return ToolContext(agent_id="t", session_id="s", channel_id="c",
                       user_id="u", vault=vault)


class FakeVault:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, key):
        return self.data.get(key)


# --- backend resolution ------------------------------------------------------

def test_api_model_requires_vault_key():
    out = sb.resolve_backend({"model": "google/gemini-2.5-flash"}, FakeVault())
    assert isinstance(out, str) and "stagehand-api-key" in out


def test_api_model_with_key_wins_over_local():
    vault = FakeVault({"secrets/stagehand-api-key": "k"})
    out = sb.resolve_backend(
        {"model": "google/gemini-2.5-flash",
         "_llm_local": {"base_url": "http://x:1/v1", "model": "m"}}, vault)
    assert out == {"kind": "api", "model": "google/gemini-2.5-flash", "api_key": "k"}


def test_local_config_used_without_probe(monkeypatch):
    monkeypatch.setattr(sb, "_probe_local", lambda: pytest.fail("should not probe"))
    out = sb.resolve_backend(
        {"_llm_local": {"base_url": "http://localhost:9999/v1", "model": "qwen"}}, None)
    assert out == {"kind": "local", "base_url": "http://localhost:9999/v1", "model": "qwen"}


def test_probe_fallback_and_no_endpoint_message(monkeypatch):
    monkeypatch.setattr(sb, "_probe_local", lambda: None)
    out = sb.resolve_backend({"_llm_local": {}}, None)
    assert isinstance(out, str) and "No model backend available" in out

    monkeypatch.setattr(sb, "_probe_local", lambda: ("ollama", "http://localhost:11434/v1"))
    monkeypatch.setattr(sb, "_first_local_model", lambda base: "qwen3.5:9b")
    out = sb.resolve_backend({"_llm_local": {}}, None)
    assert out == {"kind": "local", "base_url": "http://localhost:11434/v1",
                   "model": "qwen3.5:9b"}


# --- callback shaping helpers ------------------------------------------------

def test_unfence():
    assert sb.unfence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert sb.unfence('{"a": 1}') == '{"a": 1}'


def test_rf_schema_reads_aliased_field():
    class RF:
        def model_dump(self, **kw):
            return {"type": "json_schema", "name": "x",
                    "schema_": {"type": "object"}}
    name, schema = sb.rf_schema(RF())
    assert name == "x" and schema == {"type": "object"}
    assert sb.rf_schema(None) == ("extraction", None)


def test_part_to_text_unwraps_rootmodel_union():
    text_part = types.SimpleNamespace(root=types.SimpleNamespace(type="text", text="hi"))
    image_part = types.SimpleNamespace(root=types.SimpleNamespace(type="image"))
    assert sb.part_to_text(text_part) == "hi"
    assert "image omitted" in sb.part_to_text(image_part)

    msg = types.SimpleNamespace(role="user", content=[text_part, image_part])
    out = sb.message_to_openai(msg)
    assert out["role"] == "user" and out["content"].startswith("hi")

    single = types.SimpleNamespace(role="user", content=text_part)
    assert sb.message_to_openai(single)["content"] == "hi"


# --- schema strings ----------------------------------------------------------

def test_schema_to_model_flat_object():
    model = sb.schema_to_model(json.dumps({
        "type": "object",
        "properties": {"title": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["title"]}))
    inst = model(title="x")
    assert inst.title == "x" and inst.count is None


def test_schema_to_model_rejects_non_object():
    with pytest.raises(ValueError):
        sb.schema_to_model(json.dumps({"type": "string"}))


# --- tool validation paths (no SDK needed) -----------------------------------

async def test_unknown_action_and_missing_args():
    assert "Unknown action" in await sb.smart_browser(_ctx(), "explode")
    assert "required for goto" in await sb.smart_browser(_ctx(), "goto")
    assert "required for act" in await sb.smart_browser(_ctx(), "act")


async def test_status_and_close_without_sessions():
    assert "No smart_browser sessions" in await sb.smart_browser(_ctx(), "status")
    assert "No such session" in await sb.smart_browser(_ctx(), "close")


async def test_backend_error_reaches_caller(monkeypatch):
    monkeypatch.setattr(sb, "_config", lambda: {"_llm_local": {}})
    monkeypatch.setattr(sb, "_probe_local", lambda: None)
    out = await sb.smart_browser(_ctx(), "goto", url="https://example.com")
    assert "No model backend available" in out


async def test_ssrf_blocks_metadata_address(monkeypatch):
    # Backend resolves fine; the URL itself must be rejected before any SDK use
    monkeypatch.setattr(sb, "resolve_backend",
                        lambda cfg, vault: {"kind": "local", "base_url": "http://x/v1",
                                            "model": "m"})
    fake = types.SimpleNamespace()  # session dict stand-in never reached
    async def fake_get_session(session, cfg, vault):
        return {"page": fake, "sh": fake, "last_used": 0, "backend": {}}
    monkeypatch.setattr(sb, "_get_session", fake_get_session)
    out = await sb.smart_browser(_ctx(), "goto", url="http://169.254.169.254/meta")
    assert "blocked" in out.lower() or "not allowed" in out.lower() or "private" in out.lower()


async def test_missing_sdk_message(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_stagehand(name, *a, **k):
        if name == "stagehand" or name.startswith("stagehand."):
            raise ImportError("nope")
        return real_import(name, *a, **k)

    monkeypatch.setattr(sb, "_config", lambda: {"_llm_local": {"base_url": "http://x/v1",
                                                               "model": "m"}})
    monkeypatch.setattr(builtins, "__import__", no_stagehand)
    out = await sb.smart_browser(_ctx(), "goto", url="https://example.com")
    assert "uv sync --extra stagehand" in out
