"""OpenAI-compatible local provider — mapping, tool round-trip, detect, errors."""

import json

from src.core.base import Message, MessageRole, ToolDef, ToolParam
from src.llm.openai_compat import (
    OpenAICompatProvider,
    _strip_think,
    _to_openai_messages,
    _to_openai_tools,
)


def _provider(config=None, reply=None, capture=None):
    """Provider with HTTP monkeypatched: _request returns `reply`, probes succeed."""
    p = OpenAICompatProvider(config or {"local": {"model": "qwen3.5:9b"}})
    p._ollama_native = False   # /v1 path under test; native path has its own tests

    async def fake_check(url):
        return True

    async def fake_request(base_url, payload):
        if capture is not None:
            capture.append((base_url, payload))
        return reply or {"choices": [{"message": {"content": "hi"}}],
                         "usage": {"total_tokens": 7}, "model": "qwen3.5:9b"}

    p._check_endpoint = fake_check
    p._request = fake_request
    return p


# --- message/tool mapping ---

def test_message_mapping_tool_round_trip():
    msgs = [
        Message(role=MessageRole.SYSTEM, content="you are Atlas"),
        Message(role=MessageRole.USER, content="weather in Malmö?"),
        Message(role=MessageRole.ASSISTANT, content="",
                tool_calls=[{"id": "call_abc", "name": "weather",
                             "arguments": {"city": "Malmö"}}]),
        Message(role=MessageRole.TOOL, content="14C rain", name="weather"),
    ]
    out = _to_openai_messages(msgs)
    assert out[0] == {"role": "system", "content": "you are Atlas"}
    assert out[2]["tool_calls"][0]["id"] == "call_abc"
    assert out[2]["tool_calls"][0]["function"]["name"] == "weather"
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"city": "Malmö"}
    # TOOL message takes the id from the preceding assistant tool_call (by order)
    assert out[3] == {"role": "tool", "tool_call_id": "call_abc", "content": "14C rain"}


def test_tooldef_translation():
    async def f(ctx):
        return ""
    t = ToolDef(name="weather", description="get weather", func=f, parameters=[
        ToolParam(name="city", type="string", description="city name"),
        ToolParam(name="days", type="integer", required=False),
        ToolParam(name="units", type="string", required=False, choices=["c", "f"]),
    ])
    (spec,) = _to_openai_tools([t])
    fn = spec["function"]
    assert spec["type"] == "function" and fn["name"] == "weather"
    assert fn["parameters"]["properties"]["city"]["type"] == "string"
    assert fn["parameters"]["properties"]["units"]["enum"] == ["c", "f"]
    assert fn["parameters"]["required"] == ["city"]


# --- complete() ---

async def test_complete_returns_content_and_usage():
    captured = []
    p = _provider(capture=captured)
    resp = await p.complete([Message(role=MessageRole.USER, content="hello")])
    assert resp.content == "hi" and resp.stop_reason == "end"
    assert resp.tokens_used == 7 and resp.model == "qwen3.5:9b"
    assert captured[0][1]["model"] == "qwen3.5:9b"          # default from config


async def test_complete_parses_tool_calls_with_ids():
    reply = {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "call_1", "function": {"name": "weather",
                                      "arguments": '{"city": "Malmö"}'}}]}}]}
    p = _provider(reply=reply)
    resp = await p.complete([Message(role=MessageRole.USER, content="weather?")])
    assert resp.tool_calls == [{"id": "call_1", "name": "weather",
                                "arguments": '{"city": "Malmö"}'}]


async def test_claude_alias_never_leaks_to_local_endpoint():
    captured = []
    p = _provider(capture=captured)
    await p.complete([Message(role=MessageRole.USER, content="x")], model="sonnet")
    assert captured[0][1]["model"] == "qwen3.5:9b"           # alias replaced by local default


# --- auto-detect + errors ---

async def test_autodetect_probes_ollama_first():
    p = OpenAICompatProvider({})
    seen = []

    async def fake_check(url):
        seen.append(url)
        return url.startswith("http://localhost:1234")       # only LM Studio is up

    p._check_endpoint = fake_check
    url = await p._resolve_base_url()
    assert seen[0].startswith("http://localhost:11434")      # Ollama probed first
    assert url == "http://localhost:1234/v1"
    assert await p._resolve_base_url() == url                 # cached, no re-probe
    assert seen.count("http://localhost:11434/v1") == 1


async def test_runtime_down_returns_friendly_error():
    p = OpenAICompatProvider({})

    async def fake_check(url):
        return False

    p._check_endpoint = fake_check
    resp = await p.complete([Message(role=MessageRole.USER, content="x")])
    assert resp.stop_reason == "error"
    assert "Ollama or LM Studio" in resp.content


async def test_http_error_returns_error_response():
    p = _provider()

    async def failing_request(base_url, payload):
        raise RuntimeError("local endpoint HTTP 500: boom")

    p._request = failing_request
    resp = await p.complete([Message(role=MessageRole.USER, content="x")])
    assert resp.stop_reason == "error" and "HTTP 500" in resp.content


async def test_thinking_artifacts_stripped():
    reply = {"choices": [{"message": {
        "content": "\n\n<think>hmm the user wants pong</think>\n\npong"}}]}
    p = _provider(reply=reply)
    resp = await p.complete([Message(role=MessageRole.USER, content="x")])
    assert resp.content == "pong"


async def test_bare_reasoning_with_lone_closer_stripped():
    """Chat template pre-opens the think block — the model emits bare reasoning
    ending in </think> with no opening tag. This is the shape that leaked raw
    chain-of-thought into Discord."""
    reply = {"choices": [{"message": {
        "content": "Wait, am I Atlas? Let me trace the turns...\n</think>\n\npong"}}]}
    p = _provider(reply=reply)
    resp = await p.complete([Message(role=MessageRole.USER, content="x")])
    assert resp.content == "pong"


def test_strip_think_variants():
    assert _strip_think("<think>a</think>pong") == "pong"
    assert _strip_think("reasoning\n</think>pong") == "pong"          # lone closer
    assert _strip_think("a</think>b</think>pong") == "pong"           # several closers
    assert _strip_think("<think>a</think>mid<think>b</think> end") == "mid end"
    assert _strip_think("pong<think>trailing, never closed") == "pong"
    assert _strip_think("<think>all reasoning, no answer") == ""
    assert _strip_think("<thinking>a</thinking>pong") == "pong"       # tag variant
    assert _strip_think("<THINK>a</THINK>pong") == "pong"             # case variant
    assert _strip_think("plain answer") == "plain answer"
    assert _strip_think("") == ""


async def test_classifier_uses_ollama_native_path():
    """think/force_json route through /api/chat with think + format + keep_alive."""
    p = _provider()
    captured = {}

    async def fake_native_support(url):
        return True

    async def fake_native(base_url, payload):
        captured.update(payload)
        return {"message": {"content": '{"route": "simple", "confidence": 0.9}'}}

    p._supports_ollama_native = fake_native_support
    p._request_native = fake_native
    resp = await p.complete([Message(role=MessageRole.USER, content="hi")],
                            model="qwen3.5:2b", think=False, force_json=True)
    assert resp.content == '{"route": "simple", "confidence": 0.9}'
    assert captured["think"] is False and captured["format"] == "json"
    assert captured["keep_alive"] == "60m" and captured["model"] == "qwen3.5:2b"


async def test_non_ollama_ignores_native_kwargs():
    """LM Studio (no native API) falls through to plain /v1."""
    captured = []
    p = _provider(capture=captured)

    async def fake_native_support(url):
        return False

    p._supports_ollama_native = fake_native_support
    resp = await p.complete([Message(role=MessageRole.USER, content="hi")],
                            think=False, force_json=True)
    assert resp.content == "hi"                       # /v1 path served it
    assert "think" not in captured[0][1]              # kwargs not leaked into /v1 payload


async def test_native_full_turn_with_tools():
    """Ollama endpoints run the whole turn natively: think off, num_ctx, tools."""
    p = _provider()
    captured = {}

    async def sup(url):
        return True

    async def fake_native(base_url, payload):
        captured.update(payload)
        return {"message": {"content": "", "tool_calls": [
            {"function": {"name": "get_weather", "arguments": {"city": "Malmö"}}}]},
            "prompt_eval_count": 100, "eval_count": 20}

    p._supports_ollama_native = sup
    p._request_native = fake_native

    async def f(ctx):
        return ""
    weather = ToolDef(name="get_weather", description="w", func=f,
                      parameters=[ToolParam(name="city", type="string")])
    resp = await p.complete([Message(role=MessageRole.USER, content="weather?")],
                            tools=[weather])
    assert captured["think"] is False                          # default: thinking off
    assert captured["options"]["num_ctx"] == 16384             # not the 4096 default
    assert captured["tools"][0]["function"]["name"] == "get_weather"
    assert resp.tool_calls[0]["name"] == "get_weather"
    assert resp.tool_calls[0]["arguments"] == {"city": "Malmö"}
    assert resp.tokens_used == 120


# --- auto-pull (cache-not-install for models) ---

def _native_provider(config=None):
    p = OpenAICompatProvider(config or {"local": {"model": "qwen3.5:9b"}})

    async def sup(url):
        return True

    async def check(url):
        return True

    p._supports_ollama_native = sup
    p._check_endpoint = check
    return p


async def test_missing_model_auto_pulls_and_retries():
    p = _native_provider()
    calls = {"chat": 0, "pull": 0}

    async def fake_native(base_url, payload):
        calls["chat"] += 1
        if calls["chat"] == 1:
            raise RuntimeError('ollama native HTTP 404: {"error":"model \'qwen3.5:9b\' not found"}')
        return {"message": {"content": "hi after pull"}}

    async def fake_pull(base_url, model):
        calls["pull"] += 1

    p._request_native = fake_native
    p._request_pull = fake_pull
    resp = await p.complete([Message(role=MessageRole.USER, content="x")])
    assert resp.content == "hi after pull" and resp.stop_reason == "end"
    assert calls == {"chat": 2, "pull": 1}


async def test_auto_pull_disabled_surfaces_hint():
    p = _native_provider({"local": {"model": "qwen3.5:9b", "auto_pull": False}})

    async def fake_native(base_url, payload):
        raise RuntimeError('ollama native HTTP 404: model "qwen3.5:9b" not found')

    p._request_native = fake_native
    resp = await p.complete([Message(role=MessageRole.USER, content="x")])
    assert resp.stop_reason == "error" and "ollama pull qwen3.5:9b" in resp.content


async def test_pull_failure_surfaces_friendly_error():
    p = _native_provider()

    async def fake_native(base_url, payload):
        raise RuntimeError("ollama native HTTP 404: model not found")

    async def fake_pull(base_url, model):
        raise RuntimeError("pull failed HTTP 500: disk full")

    p._request_native = fake_native
    p._request_pull = fake_pull
    resp = await p.complete([Message(role=MessageRole.USER, content="x")])
    assert resp.stop_reason == "error" and "not found" in resp.content


async def test_concurrent_turns_pull_once():
    import asyncio as _a
    p = _native_provider()
    calls = {"chat": 0, "pull": 0}

    async def fake_native(base_url, payload):
        calls["chat"] += 1
        if calls["chat"] <= 2:
            raise RuntimeError("HTTP 404: model not found")
        return {"message": {"content": "ok"}}

    async def fake_pull(base_url, model):
        calls["pull"] += 1
        await _a.sleep(0.01)

    p._request_native = fake_native
    p._request_pull = fake_pull
    r1, r2 = await _a.gather(
        p.complete([Message(role=MessageRole.USER, content="a")]),
        p.complete([Message(role=MessageRole.USER, content="b")]))
    assert calls["pull"] == 1                          # serialized, single pull
    assert {r1.stop_reason, r2.stop_reason} == {"end"}
