"""OpenAI-compatible local LLM provider — Ollama, LM Studio, or any /v1 endpoint.

Lets an agent run on a local model (`provider: local`) instead of the Claude Code
CLI. History arrives pre-assembled from SQLite and tool execution is handled by
the engine's provider-agnostic loop (agent_manager) — this provider only maps
kbots messages/tools to the OpenAI chat format and back, so local models get
the full toolset with HITL/rate-limits enforced by the engine.

Config (under defaults.llm.local — every provider receives defaults.llm):
  local:
    base_url: http://localhost:11434/v1   # optional; auto-detects when unset
    model: qwen3.5:9b                     # default model (agent llm.model overrides)
    api_key: ""                           # optional (e.g. LM Studio API keys)
    timeout: 180

Auto-detection probes Ollama (localhost:11434) then LM Studio (localhost:1234) —
both expose the same OpenAI-compatible /v1 API, so one provider covers both and
the OS choice of runtime is just "whichever is running".
"""

import asyncio
import json
import logging
import re
import uuid

import aiohttp

from src.core.base import LLMProvider, LLMResponse, Message, ToolDef

logger = logging.getLogger(__name__)

# Probed in order when base_url isn't configured. Ollama first (battle-tested on
# Linux CPU + official MLX on Apple Silicon), then LM Studio.
_PROBE_URLS = [
    ("ollama", "http://localhost:11434/v1"),
    ("lmstudio", "http://localhost:1234/v1"),
]

# Claude Code model aliases that must not leak to a local endpoint when an agent
# uses provider "local" without setting its own llm.model.
_CLAUDE_ALIASES = {"opus", "sonnet", "haiku"}

_PARAM_TYPES = {"string": "string", "integer": "integer", "boolean": "boolean",
                "number": "number"}


def _strip_think(text: str) -> str:
    """Drop reasoning-model think content — users must never see it.

    Runtimes and chat templates disagree on the shape it arrives in: a
    well-formed <think>…</think> prefix, several blocks, an unclosed opener,
    or bare reasoning ending in a lone </think> because the template
    pre-opened the block and the model never echoed the opening tag. Every
    variant is treated as reasoning: closed blocks are removed, anything
    before a stray closer is removed, anything after a stray opener is
    removed. The cost is that prose which legitimately contains a think tag
    gets truncated — acceptable for local-model turns.
    """
    if not text:
        return ""
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text,
                  flags=re.DOTALL | re.IGNORECASE)
    # A closer left over after removing pairs is unmatched — the reasoning
    # arrived "bare" and the answer starts after the last one.
    text = re.split(r"</think(?:ing)?>", text, flags=re.IGNORECASE)[-1]
    # An unmatched opener means reasoning ran to the end of the output.
    text = re.sub(r"<think(?:ing)?>.*\Z", "", text,
                  flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def _to_openai_tools(tools: list[ToolDef]) -> list[dict]:
    """Translate kbots ToolDefs into OpenAI function-tool schemas."""
    out = []
    for t in tools:
        props: dict = {}
        required: list[str] = []
        for p in t.parameters:
            spec: dict = {"type": _PARAM_TYPES.get(p.type, "string")}
            if p.description:
                spec["description"] = p.description
            if p.choices:
                spec["enum"] = p.choices
            props[p.name] = spec
            if p.required:
                required.append(p.name)
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": {"type": "object", "properties": props,
                               "required": required},
            },
        })
    return out


def _to_openai_messages(messages: list[Message]) -> list[dict]:
    """Map internal messages to OpenAI chat format.

    Tool-call ids: complete() returns tool_calls dicts that include an "id"; the
    engine passes them back verbatim on the ASSISTANT message and appends TOOL
    results in the same order — so each TOOL message takes the next unclaimed id
    from the preceding assistant turn.
    """
    out: list[dict] = []
    pending_ids: list[str] = []
    for m in messages:
        role = getattr(m.role, "value", m.role)
        if role == "assistant" and m.tool_calls:
            calls = []
            pending_ids = []
            for i, tc in enumerate(m.tool_calls):
                cid = tc.get("id") or f"call_{i}"
                args = tc.get("arguments", "{}")
                if not isinstance(args, str):
                    args = json.dumps(args)
                calls.append({"id": cid, "type": "function",
                              "function": {"name": tc.get("name", ""), "arguments": args}})
                pending_ids.append(cid)
            out.append({"role": "assistant", "content": m.content or "",
                        "tool_calls": calls})
        elif role == "tool":
            cid = pending_ids.pop(0) if pending_ids else f"call_{uuid.uuid4().hex[:8]}"
            out.append({"role": "tool", "tool_call_id": cid, "content": m.content})
        else:
            out.append({"role": role, "content": m.content})
    return out


class OpenAICompatProvider(LLMProvider):
    """Direct-HTTP provider for OpenAI-compatible local runtimes."""
    name = "local"

    def __init__(self, config: dict):
        super().__init__(config)
        local = config.get("local", {}) or {}
        self._base_url: str | None = (local.get("base_url") or "").rstrip("/") or None
        self._configured_url = self._base_url is not None
        self._default_model = local.get("model", "")
        self._api_key = local.get("api_key", "")
        self._timeout = float(local.get("timeout", 180))
        # Reasoning models (qwen3.5…) think for MINUTES when allowed — local
        # turns handle simple requests, so thinking is off unless configured.
        self._think = bool(local.get("think", False))
        self._num_ctx = int(local.get("num_ctx", 16384))  # Ollama default 4096 truncates agent prompts
        self._keep_alive = str(local.get("keep_alive", "60m"))
        # pkgx-style cache-not-install for models: a missing model is pulled on
        # first use instead of erroring (Ollama only; auto_pull: false to disable).
        self._auto_pull = bool(local.get("auto_pull", True))
        self._pull_timeout = float(local.get("pull_timeout", 1800))
        self._pull_lock = asyncio.Lock()
        self._pulled: set[str] = set()
        self._session: aiohttp.ClientSession | None = None
        self._ollama_native: bool | None = None  # cached: base_url serves /api/chat

    # --- HTTP plumbing (separated so tests can monkeypatch) ---

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def _check_endpoint(self, base_url: str) -> bool:
        """True if an OpenAI-compatible server answers at base_url."""
        try:
            async with self._http().get(
                f"{base_url}/models", timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _resolve_base_url(self) -> str | None:
        """The configured base_url, or the first live probe target.

        Auto-detected URLs are cached; a request failure clears the cache so the
        next call re-probes (e.g. user switched from Ollama to LM Studio).
        """
        if self._base_url:
            return self._base_url
        for runtime, url in _PROBE_URLS:
            if await self._check_endpoint(url):
                logger.info(f"local provider: detected {runtime} at {url}")
                self._base_url = url
                return url
        return None

    async def _request(self, base_url: str, payload: dict) -> dict:
        async with self._http().post(
            f"{base_url}/chat/completions", json=payload,
            timeout=aiohttp.ClientTimeout(total=self._timeout),
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"local endpoint HTTP {resp.status}: {body[:300]}")
            return json.loads(body)

    async def _supports_ollama_native(self, base_url: str) -> bool:
        """True when the endpoint is Ollama (native /api/chat available). Cached."""
        if self._ollama_native is None:
            origin = base_url.removesuffix("/v1")
            try:
                async with self._http().get(
                    f"{origin}/api/version", timeout=aiohttp.ClientTimeout(total=2)
                ) as resp:
                    self._ollama_native = resp.status == 200
            except Exception:
                self._ollama_native = False
        return self._ollama_native

    async def _request_native(self, base_url: str, payload: dict) -> dict:
        """POST to Ollama's native /api/chat (thinking toggle + JSON mode)."""
        origin = base_url.removesuffix("/v1")
        async with self._http().post(
            f"{origin}/api/chat", json=payload,
            timeout=aiohttp.ClientTimeout(total=self._timeout),
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"ollama native HTTP {resp.status}: {body[:300]}")
            return json.loads(body)

    async def _request_pull(self, base_url: str, model: str) -> None:
        """Blocking model pull via Ollama's /api/pull (raises on failure)."""
        origin = base_url.removesuffix("/v1")
        async with self._http().post(
            f"{origin}/api/pull", json={"model": model, "stream": False},
            timeout=aiohttp.ClientTimeout(total=self._pull_timeout),
        ) as resp:
            body = await resp.text()
            if resp.status != 200 or '"error"' in body:
                raise RuntimeError(f"pull failed HTTP {resp.status}: {body[:200]}")

    async def _try_pull(self, base_url: str, model: str) -> bool:
        """Pull a missing model once (serialized — the router's classifier and
        workhorse can race here). True when the model should now be available."""
        async with self._pull_lock:
            if model in self._pulled:
                return True
            logger.info(f"local provider: model '{model}' missing — pulling "
                        f"(first use, may take a few minutes)…")
            try:
                await self._request_pull(base_url, model)
            except Exception as e:
                logger.warning(f"local provider: pull of '{model}' failed: {e}")
                return False
            self._pulled.add(model)
            logger.info(f"local provider: model '{model}' pulled")
            return True

    async def _complete_native(self, base_url: str, model: str,
                               messages: list[Message], tools: list[ToolDef] | None,
                               kwargs: dict) -> LLMResponse:
        """Full turn via Ollama's native /api/chat (tools, think, num_ctx)."""
        native_msgs = []
        for m in messages:
            role = getattr(m.role, "value", m.role)
            entry: dict = {"role": role, "content": m.content or ""}
            if role == "assistant" and m.tool_calls:
                entry["tool_calls"] = [
                    {"function": {"name": tc.get("name", ""),
                                  "arguments": tc.get("arguments") if isinstance(tc.get("arguments"), dict)
                                  else json.loads(tc.get("arguments") or "{}")}}
                    for tc in m.tool_calls]
            elif role == "tool" and m.name:
                entry["tool_name"] = m.name
            native_msgs.append(entry)

        payload: dict = {
            "model": model, "stream": False, "messages": native_msgs,
            "think": kwargs.get("think", self._think),
            "keep_alive": kwargs.get("keep_alive", self._keep_alive),
            "options": {"num_ctx": self._num_ctx},
        }
        if kwargs.get("force_json"):
            payload["format"] = "json"
        if tools:
            payload["tools"] = _to_openai_tools(tools)

        data = None
        for attempt in (1, 2):
            try:
                data = await self._request_native(base_url, payload)
                break
            except Exception as e:
                # Missing model → pull on first use (cache-not-install), retry once.
                if (attempt == 1 and self._auto_pull and "not found" in str(e).lower()
                        and await self._try_pull(base_url, model)):
                    continue
                if not self._configured_url:
                    self._base_url = None  # re-probe next call
                hint = (f" — run: ollama pull {model}"
                        if "not found" in str(e).lower() else "")
                return LLMResponse(
                    content=f"Local model request failed — {type(e).__name__}: {e}{hint}",
                    model=model, stop_reason="error")

        msg = data.get("message") or {}
        tool_calls = None
        if msg.get("tool_calls"):
            tool_calls = [{"id": f"call_{uuid.uuid4().hex[:8]}",
                           "name": tc.get("function", {}).get("name", ""),
                           "arguments": tc.get("function", {}).get("arguments") or {}}
                          for tc in msg["tool_calls"]]
        content = _strip_think(msg.get("content") or "")
        tokens = (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0)
        return LLMResponse(content=content, tool_calls=tool_calls,
                           tokens_used=tokens or None, model=model, stop_reason="end")

    # --- Provider interface ---

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        stream: bool = False,
        **kwargs,
    ) -> LLMResponse:
        model = kwargs.get("model") or self._default_model
        if model in _CLAUDE_ALIASES:
            # Agent inherited a Claude alias from defaults — use the local default.
            model = self._default_model or model

        base_url = await self._resolve_base_url()
        if not base_url:
            return LLMResponse(
                content=("Local model runtime not reachable — is Ollama or LM Studio "
                         "running? (probed localhost:11434 and localhost:1234)"),
                model=model, stop_reason="error",
            )

        # Ollama gets the native /api/chat for ALL turns — it's strictly better
        # than /v1 here: think=false (reasoning models like qwen3.5 otherwise
        # think for minutes with no /v1 off-switch), options.num_ctx (the /v1
        # default of 4096 silently truncates real agent prompts), format="json"
        # (constrained decoding for the tier-router classifier), keep_alive, and
        # the same tools support. LM Studio/custom endpoints use plain /v1.
        if await self._supports_ollama_native(base_url):
            return await self._complete_native(base_url, model, messages, tools, kwargs)

        payload: dict = {
            "model": model,
            "messages": _to_openai_messages(messages),
            "stream": False,
        }
        if tools:
            payload["tools"] = _to_openai_tools(tools)

        try:
            data = await self._request(base_url, payload)
        except (aiohttp.ClientError, TimeoutError) as e:
            self._base_url = self._base_url if self._configured_url else None  # re-probe next call
            return LLMResponse(
                content=f"Local model request failed: {e}",
                model=model, stop_reason="error",
            )
        except (RuntimeError, json.JSONDecodeError) as e:
            return LLMResponse(content=str(e), model=model, stop_reason="error")

        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError):
            return LLMResponse(content=f"Malformed response from local endpoint: {str(data)[:200]}",
                               model=model, stop_reason="error")

        tool_calls = None
        if msg.get("tool_calls"):
            tool_calls = []
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                tool_calls.append({
                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "{}"),
                })

        content = _strip_think(msg.get("content") or "")

        usage = data.get("usage") or {}
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            tokens_used=usage.get("total_tokens"),
            model=data.get("model", model),
            stop_reason="end",
        )
