"""smart_browser — natural-language browser automation via Stagehand.

Sits alongside the `browser` (headless Playwright) and `chrome_browser`
(real Chrome) tools: instead of CSS selectors, you say what you want —
act("click the login button"), extract structured data against a schema,
observe what's actionable. Stagehand (MIT, browserbase/stagehand) drives
its own local Chromium; nothing touches Browserbase cloud.

The AI inside Stagehand needs a model. Default: your LOCAL OpenAI-compatible
endpoint (Ollama / LM Studio — the same stack the tier router uses), wired
through a custom LLM callback, so it costs nothing and nothing leaves the
machine. Local models are honest-but-mediocre at Stagehand's structured
outputs: `extract` works well, `act` sometimes answers "no action found".
For stronger results set an API model: vault key `secrets/stagehand-api-key`
plus config `stagehand.model` (e.g. "google/gemini-2.5-flash").

Config (config.yaml, all optional):
    stagehand:
      model: ""            # e.g. google/gemini-2.5-flash — needs the vault key
      local_model: ""      # override which local model the callback uses
      base_url: ""         # override the local endpoint
      headless: true

SSRF note: Stagehand's page has no request-interception hook, so unlike
`browser` there is no per-request guard. Every navigation target is
validated up front (src/lib/ssrf.validate_url) and the page URL is
re-validated after each act/goto; redirects to blocked ranges close the
session. Treat this tool as slightly weaker than `browser` for hostile
pages.
"""

import asyncio
import json
import logging
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from src.core.base import KBOTS_TMP, ToolContext, resolve_config_file
from src.core.tools import tool
from src.lib.ssrf import validate_url as _validate_url

logger = logging.getLogger(__name__)

VALID_ACTIONS = ("goto", "act", "observe", "extract", "screenshot", "status", "close")
_SESSION_TTL = 300  # idle seconds before a session is reaped
_LLM_TIMEOUT = 300  # local models are slow; generous per-generation budget
_PROBE_URLS = (("ollama", "http://localhost:11434/v1"),
               ("lmstudio", "http://localhost:1234/v1"))

_sessions: dict[str, dict] = {}


# --- config -----------------------------------------------------------------

def _config() -> dict:
    """The stagehand: section of config.yaml plus defaults.llm.local."""
    try:
        raw = yaml.safe_load(Path(resolve_config_file("config.yaml")).read_text()) or {}
    except Exception:
        raw = {}
    cfg = dict(raw.get("stagehand") or {})
    cfg["_llm_local"] = ((raw.get("defaults") or {}).get("llm") or {}).get("local") or {}
    return cfg


def _probe_local() -> tuple[str, str] | None:
    """(kind, base_url) of a live local OpenAI-compatible endpoint, else None."""
    for kind, base in _PROBE_URLS:
        try:
            with urllib.request.urlopen(f"{base}/models", timeout=3) as resp:
                if resp.status == 200:
                    return kind, base
        except Exception:
            continue
    return None


def _first_local_model(base_url: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{base_url}/models", timeout=3) as resp:
            data = json.loads(resp.read())
        models = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return models[0] if models else None
    except Exception:
        return None


def resolve_backend(cfg: dict, vault) -> dict | str:
    """Decide how Stagehand's AI calls run. Returns a backend dict or an
    error string explaining exactly what to configure.

    Order: explicit API model (config stagehand.model + vault key) →
    local endpoint from config → live probe (Ollama, then LM Studio).
    """
    api_model = (cfg.get("model") or "").strip()
    if api_model:
        key = vault.get("secrets/stagehand-api-key") if vault else None
        if not key:
            return ("stagehand.model is set but vault key "
                    "'secrets/stagehand-api-key' is missing — add it with "
                    "vault-manage.py, or unset stagehand.model to use a local model.")
        return {"kind": "api", "model": api_model, "api_key": key}

    local = cfg.get("_llm_local") or {}
    base_url = (cfg.get("base_url") or local.get("base_url") or "").strip().rstrip("/")
    model = (cfg.get("local_model") or local.get("model") or "").strip()
    if not base_url:
        probed = _probe_local()
        if not probed:
            return ("No model backend available: no local OpenAI-compatible "
                    "endpoint found (Ollama on :11434 / LM Studio on :1234), "
                    "and no stagehand.model API override configured. Start a "
                    "local model server or set stagehand.model + the "
                    "'secrets/stagehand-api-key' vault key.")
        base_url = probed[1]
    if not model:
        model = _first_local_model(base_url) or ""
        if not model:
            return (f"Local endpoint {base_url} is up but reports no models — "
                    f"pull one (e.g. `ollama pull qwen3.5:9b`) or set "
                    f"stagehand.local_model.")
    return {"kind": "local", "base_url": base_url, "model": model}


# --- LLM callback (local backend) -------------------------------------------
# Shapes learned from a live spike against stagehand 4.0.1 + Ollama:
# message content blocks are RootModel unions (.root → text/image/tool_*);
# response_format dumps its schema under the alias 'schema_'; Ollama's
# json_schema grammar compiler rejects some Stagehand schemas (fall back to
# schema-in-prompt + json_object); local models fence JSON in markdown.

def unfence(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return m.group(1) if m else text


def rf_schema(rf) -> tuple[str, dict | None]:
    """(name, json-schema) out of Stagehand's response_format model."""
    if rf is None:
        return "extraction", None
    d = rf if isinstance(rf, dict) else rf.model_dump(
        exclude_none=True, mode="json", by_alias=True)
    return (d.get("name") or "extraction"), (d.get("schema") or d.get("schema_"))


def part_to_text(part) -> str:
    r = getattr(part, "root", part)
    kind = getattr(r, "type", None)
    if kind == "text" or hasattr(r, "text"):
        return getattr(r, "text", "") or ""
    if kind == "image":
        return "[image omitted — text-only model]"
    return f"[{kind or type(r).__name__}]"


def message_to_openai(m) -> dict:
    content = m.content
    if isinstance(content, list):
        content = "\n".join(part_to_text(p) for p in content)
    elif not isinstance(content, str):
        content = part_to_text(content)
    role = getattr(m.role, "value", None) or str(m.role)
    return {"role": role, "content": content}


def _chat(base_url: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{base_url}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_LLM_TIMEOUT) as resp:
        return json.loads(resp.read())["choices"][0]["message"]


def chat_structured(base_url: str, model: str, msgs: list, name: str, schema: dict) -> dict:
    body = {"model": model, "messages": msgs, "stream": False, "temperature": 0,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": name, "schema": schema}}}
    try:
        return _chat(base_url, body)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        e.read()
        # Constrained decoding rejected the schema — steer by prompt instead.
        prompted = [*msgs, {"role": "user", "content":
                    "Respond with ONLY a JSON object that validates against this "
                    "JSON schema — every required property, exact types, no extra "
                    "keys:\n" + json.dumps(schema)}]
        return _chat(base_url, {"model": model, "messages": prompted, "stream": False,
                                "temperature": 0,
                                "response_format": {"type": "json_object"}})


def make_local_callback(base_url: str, model: str):
    """An LLMGenerateCallback bridging Stagehand to a local OpenAI endpoint."""

    async def callback(params):
        from stagehand._generated.models import LLMStructuredGenerateParams
        structured = isinstance(params, LLMStructuredGenerateParams)
        msgs = []
        if params.system_prompt:
            msgs.append({"role": "system", "content": params.system_prompt})
        msgs.extend(message_to_openai(m) for m in params.messages)
        name, schema = rf_schema(getattr(params, "response_format", None))
        if structured and schema:
            reply = await asyncio.to_thread(
                chat_structured, base_url, model, msgs, name, schema)
        else:
            reply = await asyncio.to_thread(
                _chat, base_url, {"model": model, "messages": msgs,
                                  "stream": False, "temperature": 0})
        text = reply.get("content") or ""
        if structured:
            return {"role": "assistant", "content": [{"type": "text", "text": text}],
                    "stop_reason": "stop", "output_format": "json_schema",
                    "structured_content": json.loads(unfence(text))}
        return {"role": "assistant", "content": [{"type": "text", "text": text}],
                "stop_reason": "stop", "output_format": "text"}

    return callback


# --- schema strings → pydantic (extract) ------------------------------------

def schema_to_model(schema_json: str):
    """A pydantic model from a flat JSON-schema string (object of simple types)."""
    from pydantic import create_model
    schema = json.loads(schema_json)
    props = schema.get("properties")
    if schema.get("type") != "object" or not isinstance(props, dict) or not props:
        raise ValueError("schema must be a JSON schema object with 'properties'")
    py = {"string": str, "number": float, "integer": int, "boolean": bool,
          "array": list, "object": dict}
    required = set(schema.get("required") or props.keys())
    fields = {}
    for key, spec in props.items():
        typ = py.get((spec or {}).get("type", "string"), str)
        fields[key] = (typ, ...) if key in required else (typ | None, None)
    return create_model("Extraction", **fields)


# --- sessions ---------------------------------------------------------------

def _cleanup_stale() -> None:
    now = time.time()
    for key in [k for k, s in _sessions.items()
                if now - s.get("last_used", 0) > _SESSION_TTL]:
        sess = _sessions.pop(key)
        for closer in ("sh", "browser"):
            obj = sess.get(closer)
            if obj is not None:
                try:
                    asyncio.get_event_loop().create_task(obj.close())
                except Exception:
                    pass


async def _get_session(session: str, cfg: dict, vault) -> dict | str:
    _cleanup_stale()
    sess = _sessions.get(session)
    if sess:
        sess["last_used"] = time.time()
        return sess

    backend = resolve_backend(cfg, vault)
    if isinstance(backend, str):
        return backend

    try:
        from stagehand import Stagehand, local_browser
    except ImportError:
        return ("Error: stagehand not installed. Run: uv sync --extra stagehand "
                "(then restart the service).")

    if backend["kind"] == "local":
        model = make_local_callback(backend["base_url"], backend["model"])
        create_kwargs = {"model": model}
    else:
        create_kwargs = {"model": backend["model"],
                         "model_api_key": backend["api_key"]}

    browser = await local_browser.launch(headless=bool(cfg.get("headless", True)))
    try:
        sh = await Stagehand.create(browser=browser, **create_kwargs)
        page = await browser.context.new_page()
    except Exception:
        try:
            await browser.close()
        except Exception:
            pass
        raise
    sess = {"browser": browser, "sh": sh, "page": page,
            "backend": backend, "last_used": time.time()}
    _sessions[session] = sess
    return sess


async def _close_session(session: str) -> bool:
    sess = _sessions.pop(session, None)
    if not sess:
        return False
    for closer in ("sh", "browser"):
        obj = sess.get(closer)
        if obj is not None:
            try:
                await obj.close()
            except Exception:
                pass
    return True


async def _page_url(page) -> str:
    """page.url is an ASYNC method on Stagehand's page wrapper (verified live:
    calling it returns a coroutine — treating it as a string was the bug the
    first live test caught)."""
    url = page.url
    if callable(url):
        url = url()
    if asyncio.iscoroutine(url):
        url = await url
    return str(url)


async def _guard_landed_url(session: str, page) -> str | None:
    """Post-navigation SSRF check — no request hook exists, so verify where
    we ended up and burn the session if it's a blocked address."""
    landed = await _page_url(page)
    if landed.startswith("about:"):
        return None
    err = _validate_url(landed)
    if err:
        await _close_session(session)
        return (f"Blocked: the page navigated to a disallowed address "
                f"({landed}); session closed. {err}")
    return None


# --- the tool ---------------------------------------------------------------

@tool(
    name="smart_browser",
    description=(
        "AI-native browser (Stagehand): describe what you want in natural "
        "language instead of CSS selectors. Actions: goto a URL, act "
        "('click the login button'), observe (list what's actionable), "
        "extract (structured data matching a JSON schema), screenshot, "
        "status, close. Runs its own local Chromium; the AI runs on the "
        "local model endpoint by default. For sites that block automation "
        "or need your logins, use chrome_browser instead."
    ),
    category="browser",
)
async def smart_browser(ctx: ToolContext, action: str, url: str = "",
                        instruction: str = "", schema: str = "",
                        session: str = "default", max_length: int = 15000) -> str:
    """Natural-language browser automation via Stagehand.

    Use this over `browser` when a page is messy or selectors are unknown:
    say the goal ("click the blue Submit button", "extract all product names
    and prices") and Stagehand finds the elements. It is slower than
    `browser` (each act/extract is an extra model call) and the default
    local model is fallible — if act answers "no action found", either try
    a more specific instruction or fall back to `browser` with selectors.

    Args:
        action: goto, act, observe, extract, screenshot, status, close.
        url: page URL (for 'goto').
        instruction: natural-language instruction (act/observe/extract).
        schema: JSON-schema string for 'extract' — a flat object, e.g.
            '{"type":"object","properties":{"title":{"type":"string"}}}'.
            Empty = free-form extraction.
        session: named session; parallel sessions get separate browsers.
        max_length: max characters returned for extract/observe results.
    """
    action = action.lower().strip()
    if action not in VALID_ACTIONS:
        return f"Unknown action: {action}. Valid: {', '.join(VALID_ACTIONS)}"

    if action == "status":
        if not _sessions:
            return "No smart_browser sessions open."
        lines = []
        for name, sess in _sessions.items():
            backend = sess.get("backend", {})
            lines.append(f"- '{name}': {await _page_url(sess['page'])} "
                         f"(model: {backend.get('model', '?')}, "
                         f"idle {int(time.time() - sess['last_used'])}s)")
        return "Open sessions:\n" + "\n".join(lines)

    if action == "close":
        return ("Session closed." if await _close_session(session)
                else "No such session.")

    if action == "goto" and not url:
        return "Error: 'url' required for goto."
    if action in ("act", "observe", "extract") and not instruction:
        return f"Error: 'instruction' required for {action}."

    try:
        cfg = _config()
        sess = await _get_session(session, cfg, ctx.vault)
        if isinstance(sess, str):
            return sess
        page, sh = sess["page"], sess["sh"]

        if action == "goto":
            err = _validate_url(url)
            if err:
                return err
            await page.goto(url)
            blocked = await _guard_landed_url(session, page)
            if blocked:
                return blocked
            title = await page.title()
            url_now = await _page_url(page)
            return f"Opened **{title or url_now}** | URL: {url_now}"

        if action == "act":
            result = await sh.act(instruction, page=page)
            blocked = await _guard_landed_url(session, page)
            if blocked:
                return blocked
            data = getattr(result, "data", result)
            ok = getattr(data, "success", None)
            msg = getattr(data, "message", "") or str(data)
            hint = ("" if ok else
                    "\nHint: the local model could not find the action — try a "
                    "more specific instruction, use `browser` with a CSS "
                    "selector, or configure stagehand.model + the "
                    "'secrets/stagehand-api-key' vault key for a stronger model.")
            return f"Act {'succeeded' if ok else 'FAILED'}: {msg}" \
                   f"\nURL now: {await _page_url(page)}{hint}"

        if action == "observe":
            result = await sh.observe(instruction, page=page)
            text = str(getattr(result, "data", result))
            return text[:max_length] + ("" if len(text) <= max_length else "\n[truncated]")

        if action == "extract":
            kwargs = {"page": page, "screenshot": False}
            if schema:
                try:
                    kwargs["schema"] = schema_to_model(schema)
                except (ValueError, json.JSONDecodeError) as e:
                    return f"Error: invalid schema — {e}"
            result = await sh.extract(instruction, **kwargs)
            data = getattr(result, "data", result)
            dumped = (data.model_dump_json(indent=2)
                      if hasattr(data, "model_dump_json") else str(data))
            return dumped[:max_length] + ("" if len(dumped) <= max_length else "\n[truncated]")

        if action == "screenshot":
            media_dir = KBOTS_TMP / "media"
            media_dir.mkdir(parents=True, exist_ok=True)
            fd, path = tempfile.mkstemp(
                prefix=f"smart_browser_{session}_", suffix=".png", dir=str(media_dir))
            import os as _os
            _os.close(fd)
            data = await page.screenshot()
            Path(path).write_bytes(data)
            title = await page.title()
            return f"Screenshot saved: {path}\nPage: **{title}** | URL: {await _page_url(page)}"

    except Exception as e:
        return f"Browser error: {e}"

    return f"Unhandled action: {action}"
