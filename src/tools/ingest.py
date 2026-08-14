"""Ingestion tools — create skills and tools, add MCP servers, ingest URLs.

These tools let agents create new capabilities from conversation:
  - create_skill: YAML prompt templates — safe, declarative, composable.
  - create_tool:  Python @tool functions — real code, so it is HITL-gated
    (a human reads the source in the approval prompt before it goes live)
    and statically validated against an AST deny-list. Written to the
    overlay's tools/ directory and hot-reloaded immediately.
"""

import ast
import asyncio
import http.client
import ipaddress
import logging
import os
import socket
import ssl
from functools import partial
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from src.core.base import KBOTS_TMP, PROJECT_ROOT, ToolContext
from src.core.tools import get_all_tools, tool

logger = logging.getLogger(__name__)

_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


_PROJECT_ROOT = PROJECT_ROOT
_OVERLAY = os.environ.get("KBOTS_OVERLAY", "")

ALLOWED_PATH_ROOTS = (
    Path("/tmp"),
    KBOTS_TMP,
    _PROJECT_ROOT / "agents",
    _PROJECT_ROOT / "data",
    _PROJECT_ROOT / "codex",
    _PROJECT_ROOT / "skills",
    *(
        (Path(_OVERLAY),)
        if _OVERLAY
        else ()
    ),
)


def validate_file_path(file_path: str) -> str | None:
    """Validate a file path is within allowed directories. Returns error string or None if OK."""
    try:
        resolved = Path(file_path).resolve()
    except (ValueError, OSError):
        return f"Invalid file path: {file_path}"

    for root in ALLOWED_PATH_ROOTS:
        try:
            resolved.relative_to(root)
            return None
        except ValueError:
            continue

    return f"Access denied: {file_path} is outside allowed directories"


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if the address falls in a blocked net.

    IPv4-mapped IPv6 addresses (::ffff:a.b.c.d) are also checked as their
    embedded IPv4 address — ipaddress never matches v4 nets against v6
    addresses, so a naive ::ffff:0:0/96 net entry would not catch them.
    """
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        candidates.append(mapped)
    return any(c in net for c in candidates for net in _BLOCKED_NETS)


def _validate_url(url: str) -> tuple[str | None, str | None]:
    """Validate a URL is safe to fetch. Returns (error, resolved_ip).

    If error is not None, the URL is blocked.
    If error is None, resolved_ip contains the first safe IP to connect to
    (use this to prevent DNS rebinding — resolve once, pin the IP).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Blocked scheme: {parsed.scheme}. Only http/https allowed.", None
    hostname = parsed.hostname
    if not hostname:
        return "No hostname in URL.", None
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return f"Cannot resolve hostname: {hostname}", None
    first_safe_ip = None
    for _, _, _, _, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        if _ip_blocked(ip):
            return f"Blocked: {hostname} resolves to internal address {ip}.", None
        if first_safe_ip is None:
            first_safe_ip = str(ip)
    return None, first_safe_ip


# --- SSRF-safe fetching: pin the validated IP, re-validate every redirect hop ---

class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that dials a pre-validated IP instead of re-resolving DNS."""

    def __init__(self, host, pinned_ip: str, **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that dials a pinned IP but keeps SNI + cert checks on the hostname."""

    def __init__(self, host, pinned_ip: str, **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address,
        )
        # server_hostname = the original host, so SNI and certificate
        # validation behave exactly as if we had dialed the hostname.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPHandler(HTTPHandler):
    def __init__(self, pinned_ip: str):
        super().__init__()
        self._pinned_ip = pinned_ip

    def http_open(self, req):
        return self.do_open(partial(_PinnedHTTPConnection, pinned_ip=self._pinned_ip), req)


class _PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, pinned_ip: str):
        super().__init__(context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def https_open(self, req):
        return self.do_open(
            partial(_PinnedHTTPSConnection, pinned_ip=self._pinned_ip, context=self._context),
            req,
        )


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse automatic redirects — each hop must be re-validated by _safe_urlopen."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_REDIRECT_CODES = (301, 302, 303, 307, 308)


def _safe_urlopen(url: str, *, timeout: float, headers: dict | None = None, max_redirects: int = 5):
    """urlopen with SSRF protection: validates each URL, connects to the
    validated IP (no re-resolve — defeats DNS rebinding), and re-validates
    every redirect hop manually.

    Returns the final response object; raises URLError/HTTPError like urlopen.
    """
    headers = headers or {}
    cur_url = url
    for _ in range(max_redirects + 1):
        err, pinned_ip = _validate_url(cur_url)
        if err:
            raise URLError(err)
        opener = build_opener(
            ProxyHandler({}),  # no env proxies — they would bypass the IP pin
            _NoRedirectHandler(),
            _PinnedHTTPHandler(pinned_ip),
            _PinnedHTTPSHandler(pinned_ip),
        )
        try:
            return opener.open(Request(cur_url, headers=headers), timeout=timeout)
        except HTTPError as e:
            if e.code not in _REDIRECT_CODES:
                raise
            location = e.headers.get("Location")
            if e.fp is not None:
                e.close()
            if not location:
                raise URLError(f"{e.code} redirect without a Location header") from None
            cur_url = urljoin(cur_url, location)
    raise URLError(f"Too many redirects (>{max_redirects}).")


@tool(
    name="create_skill",
    description=(
        "Create a new skill from conversation — becomes a slash command.\n"
        "WRITE AN OPERABLE PROMPT: skills are often run by a SMALL local model "
        "(run_on_local=True), so the prompt is an operating manual, not an "
        "essay: numbered steps, ONE tool call per step; the last step states "
        "what done looks like; require a tool call for every factual claim — "
        "never answer from recall; check the tool RESULT before confirming "
        "(verify by observation, not inference); confirm in ONE short "
        "sentence; keep the whole prompt under ~10 lines; for each {param}, "
        "name its valid values in the prompt text."
    ),
    category="system",
)
async def create_skill(
    ctx: ToolContext,
    name: str,
    description: str,
    prompt: str,
    tools: str = "memory_search",
    run_on_local: bool = False,
    local_model: str = "",
    max_rounds: int = 0,
) -> str:
    """Create a reusable skill that becomes available as a /slash command.

    Args:
        name: Skill name (lowercase, underscores ok, e.g. 'competitor_analysis')
        description: Short description shown in Discord
        prompt: The prompt template. Use {param_name} for parameters.
        tools: Comma-separated tool names the skill needs
        run_on_local: pin this skill's turns to the LOCAL model with the tool
            list as its ONLY tools (restrict_tools) — the pattern for repetitive
            tasks: a large model authors the tool + this skill once, the local
            model operates it from then on (zero subscription cost; falls back
            to the default provider if the local runtime errors).
        local_model: local model override (default: the configured local model)
        max_rounds: tool-round cap for the skill (recommended 3-6 for local)
    """
    from src.core.digest import ingest_skill_from_text

    # Validate name
    clean_name = name.lower().replace(" ", "_").replace("-", "_")
    if not clean_name.isidentifier():
        return f"Invalid skill name: {name}. Use lowercase letters, numbers, underscores."

    tool_list = [t.strip() for t in tools.split(",") if t.strip()]

    # Validate tools exist
    all_tools = get_all_tools()
    unknown = [t for t in tool_list if t not in all_tools]
    if unknown:
        return f"Unknown tools: {unknown}. Available: {list(all_tools.keys())}"

    # Extract parameters from {placeholders} in prompt
    import re
    params = [p for p in re.findall(r"\{(\w+)\}", prompt) if p.isidentifier()]
    parameters = {}
    if params:
        for p in set(params):
            parameters[p] = {
                "type": "string",
                "description": p.replace("_", " ").title(),
                "required": True,
            }

    llm = None
    if run_on_local:
        llm = {"provider": "local"}
        if local_model:
            llm["model"] = local_model

    path = ingest_skill_from_text(
        name=clean_name,
        description=description,
        prompt=prompt,
        tools=tool_list,
        parameters=parameters if parameters else None,
        llm=llm,
        restrict_tools=run_on_local,
        max_rounds=max_rounds,
    )

    param_str = ", ".join(f"`{p}`" for p in parameters) if parameters else "none"
    local_str = (f"\nRuns on the LOCAL model ({local_model or 'configured default'}), "
                 f"toolset restricted to the list above"
                 + (f", max {max_rounds} tool rounds" if max_rounds else "")
                 if run_on_local else "")
    warn = ""
    if run_on_local and len(prompt.strip().splitlines()) > 12:
        warn = ("\nWARNING: prompt is >12 lines — long prompts degrade "
                "local-model operation; consider trimming.")
    return (
        f"Skill '{clean_name}' created at {path}\n"
        f"Parameters: {param_str}\n"
        f"Tools: {', '.join(tool_list)}{local_str}{warn}\n"
        f"Available as `/{clean_name.replace('_', '-')}` after next command sync."
    )


# --- create_tool: agents author real Python tools (HITL-gated) ---

_FORBIDDEN_IMPORTS = {"subprocess", "ctypes", "pty", "multiprocessing", "importlib"}
_FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
_FORBIDDEN_OS_ATTRS = {
    "system", "popen", "execv", "execve", "execvp", "spawnl", "spawnv",
    "fork", "kill", "remove", "unlink", "rmdir", "removedirs",
}


def _validate_tool_source(code: str) -> str | None:
    """Static safety check for agent-authored tool code.

    Returns an error string, or None if the code passes. This is a guardrail,
    not a sandbox — the real trust boundary is the HITL approval where a
    human reads the source.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Syntax error at line {e.lineno}: {e.msg}"

    has_tool_def = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) \
                else [node.module or ""]
            for mod in names:
                root = mod.split(".")[0]
                if root in _FORBIDDEN_IMPORTS:
                    return f"Forbidden import: {root}"
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALLS:
                return f"Forbidden call: {func.id}()"
            if (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                    and func.attr in _FORBIDDEN_OS_ATTRS):
                return f"Forbidden call: os.{func.attr}()"
            if (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "shutil"
                    and func.attr in ("rmtree", "move")):
                return f"Forbidden call: shutil.{func.attr}()"
        elif isinstance(node, ast.AsyncFunctionDef) or isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Name) and target.id == "tool":
                    has_tool_def = True

    if not has_tool_def:
        return ("No @tool-decorated function found. The code must define an "
                "async function decorated with @tool(name=..., description=...).")
    return None


@tool(
    name="create_tool",
    description=(
        "Create a new Python tool from source code. The code must define an "
        "async function decorated with @tool(name=..., description=...) that "
        "takes ctx as first argument. Requires human approval (the source is "
        "shown in the approval prompt). On approval the tool is hot-loaded "
        "immediately and available to all agents — no restart.\n"
        "DESIGN FOR LOCAL OPERATION: tools are often driven by a SMALL local "
        "model afterwards (via a run_on_local skill), so keep them operable: "
        "1-3 params max; constrain values with "
        "Annotated[str, {'choices': [...]}] enums; return ONE terse result "
        "line; on bad input, return an error that LISTS the valid options so "
        "the model self-corrects; never take raw hosts/URLs/secrets as params "
        "— read them from config or the vault inside the tool."
    ),
    category="system",
    hitl=True,
)
async def create_tool(
    ctx: ToolContext,
    name: str,
    description: str,
    python_code: str,
) -> str:
    """Author a new tool as Python code and load it live.

    name: file/tool name (lowercase identifier). description: what it does.
    python_code: complete module source, e.g.:

        from src.core.base import ToolContext
        from src.core.tools import tool

        @tool(name="my_tool", description="...")
        async def my_tool(ctx: ToolContext, arg: str) -> str:
            return f"result for {arg}"
    """
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    if not overlay:
        return "ERROR: KBOTS_OVERLAY is not set — cannot locate the overlay tools directory."

    # 'mcp-agent' is the MCP server's shared fallback when the agent's
    # .mcp.json lacks identity env vars. Recording ownership under it would
    # make a "private" tool effectively shared by every mis-configured agent.
    if ctx.agent_id == "mcp-agent":
        return (
            "ERROR: cannot create a tool under the shared fallback identity "
            "'mcp-agent' — ownership and privacy would be meaningless. The "
            "agent's .mcp.json is missing KBOTS_AGENT_ID/KBOTS_PROJECT_DIR "
            "in the kbots-tools env block; ask the operator to regenerate it."
        )

    clean_name = name.lower().replace(" ", "_").replace("-", "_")
    if not clean_name.isidentifier():
        return f"ERROR: invalid tool name: {name!r}. Use lowercase letters, digits, underscores."

    # Compose the final module and validate THAT, not just python_code. The
    # description becomes the module docstring, so a naive f-string would let a
    # triple-quote in description close the docstring and inject module-level
    # code that bypasses the AST guard. repr() emits a safe single-line string
    # literal that cannot break out; validating the composed source is the
    # belt-and-braces check.
    doc = repr(f"{description}\n\nCreated by agent {ctx.agent_id} via create_tool.")
    composed = f"{doc}\n\n{python_code.strip()}\n"

    error = _validate_tool_source(composed)
    if error:
        return f"ERROR: code rejected — {error}"

    existing = get_all_tools()
    if clean_name in existing:
        return f"ERROR: a tool named '{clean_name}' already exists."

    tools_dir = Path(overlay) / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    tool_path = tools_dir / f"{clean_name}.py"
    if tool_path.exists():
        return f"ERROR: {tool_path} already exists."

    tool_path.write_text(composed)

    # Hot-load immediately and verify the import actually registered something
    from src.core.digest import reload_tools
    before = set(existing)
    try:
        reload_tools()
    except Exception as e:
        tool_path.unlink(missing_ok=True)
        return f"ERROR: reload failed ({e}) — file removed."
    new_tools = set(get_all_tools()) - before

    if not new_tools:
        tool_path.unlink(missing_ok=True)
        return (
            "ERROR: the code imported but registered no new tool (import may "
            "have failed — check the decorator). File removed; check logs and try again."
        )

    # Agent-created tools are private to their creator until promoted
    from src.core.tool_scope import record_tool
    for t in new_tools:
        record_tool(t, ctx.agent_id)

    logger.info(f"create_tool: {tool_path.name} by agent {ctx.agent_id} → {sorted(new_tools)}")
    return (
        f"Tool created: {tool_path}\n"
        f"Live now (hot-reloaded, no restart): {', '.join(sorted(new_tools))}\n"
        f"Scope: PRIVATE to you ({ctx.agent_id}). If another agent needs it, "
        f"promote it with promote_tool."
    )


@tool(
    name="promote_tool",
    description=(
        "Promote an agent-created tool from private (creator-only) to global "
        "(usable by all agents). Only the tool's owner, the coordinator, or a "
        "privileged agent can promote. Requires human approval."
    ),
    category="system",
    hitl=True,
)
async def promote_tool(ctx: ToolContext, name: str) -> str:
    """Make a private agent-created tool available to all agents."""
    from src.core.agent_scaffold import agent_is_privileged_or_coordinator
    from src.core.tool_scope import get_entry, promote_to_global

    entry = get_entry(name)
    if entry is None:
        if name in get_all_tools():
            return f"'{name}' is not an agent-created tool — it is already global by nature."
        return f"ERROR: no tool named '{name}' found."

    if entry.get("global"):
        return f"'{name}' is already global."

    owner = entry.get("owner", "")
    overlay = os.environ.get("KBOTS_OVERLAY", "")
    is_admin = overlay and agent_is_privileged_or_coordinator(Path(overlay), ctx.agent_id)
    if ctx.agent_id != owner and not is_admin:
        return (
            f"ERROR: '{name}' is owned by agent '{owner}'. Only the owner, the "
            f"coordinator, or a privileged agent can promote it — ask them."
        )

    promote_to_global(name)
    logger.info(f"promote_tool: '{name}' (owner {owner}) promoted by {ctx.agent_id}")
    return f"Tool '{name}' is now GLOBAL — available to all agents immediately."


@tool(
    name="read_url",
    description="Read content from a URL",
    category="research",
)
async def read_url(ctx: ToolContext, url: str, max_length: int = 10000) -> str:
    """Fetch and return the text content of a URL.

    Useful for ingesting skill definitions, documentation, or data.
    """
    err, _resolved_ip = _validate_url(url)
    if err:
        return err
    try:
        with _safe_urlopen(url, timeout=15, headers={"User-Agent": "kbots/0.1"}) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(max_length + 1000)

        text = raw.decode("utf-8", errors="replace")

        # Basic HTML stripping for web pages
        if "html" in content_type.lower():
            import re
            text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()

        if len(text) > max_length:
            text = text[:max_length] + "\n\n[truncated]"

        return text

    except URLError as e:
        return f"Failed to fetch URL: {e}"
    except Exception as e:
        return f"Error reading URL: {e}"


@tool(
    name="download_file",
    description=("Download a file from a URL to a local temp path. Returns the file "
                 "path for use with Claude's Read tool (images, PDFs) or other processing."),
    category="research",
)
async def download_file(ctx: ToolContext, url: str, filename: str | None = None) -> str:
    """Download a file (image, PDF, etc.) from a URL and save it locally.

    Returns the local file path. Use Claude's built-in Read tool to view
    downloaded images or PDFs.
    """
    import tempfile

    err, _resolved_ip = _validate_url(url)
    if err:
        return err

    max_size = 50 * 1024 * 1024  # 50 MB limit

    try:
        with _safe_urlopen(url, timeout=30, headers={"User-Agent": "kbots/0.1"}) as resp:
            data = resp.read(max_size + 1)
            if len(data) > max_size:
                return f"File too large (>{max_size // 1024 // 1024} MB)"

            # Determine filename
            if not filename:
                from urllib.parse import unquote
                path = urlparse(url).path
                filename = unquote(path.split("/")[-1]) or "download"
                # Strip query params that got into filename
                if "?" in filename:
                    filename = filename.split("?")[0]

            suffix = Path(filename).suffix or ""
            # Save into the calling agent's OWN workspace so folder-confined
            # (assistant-tier) agents can Read it back — Read(./**) covers files
            # under project_dir. Also keeps each agent's downloads isolated
            # (matters for e.g. a finance agent's statements). Falls back to
            # shared scratch only when there's no project_dir.
            if ctx.project_dir:
                dl_dir = Path(ctx.project_dir) / "downloads"
            else:
                dl_dir = KBOTS_TMP / "scratch"
            dl_dir.mkdir(parents=True, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=suffix, prefix="kbots-dl-",
                dir=str(dl_dir),
            )
            tmp.write(data)
            tmp.close()

            return f"Downloaded to: {tmp.name} ({len(data)} bytes)"

    except URLError as e:
        return f"Failed to download: {e}"
    except Exception as e:
        return f"Error downloading file: {e}"


@tool(
    name="browse_url",
    description="Browse a URL with a full browser — renders JavaScript, handles SPAs and dynamic content",
    category="research",
)
async def browse_url(
    ctx: ToolContext,
    url: str,
    wait_seconds: int = 3,
    max_length: int = 15000,
) -> str:
    """Fetch a URL using a headless browser with full JS rendering.

    Use this instead of read_url when:
    - The page uses JavaScript to load content (SPAs, React, etc.)
    - read_url returns empty/useless content
    - You need content that loads dynamically

    Args:
        url: The URL to browse
        wait_seconds: Seconds to wait for JS to render (1-10, default 3)
        max_length: Max characters to return (default 15000)
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return "Error: playwright not installed. Run: uv add playwright && playwright install chromium"

    err, _resolved_ip = _validate_url(url)
    if err:
        return err

    wait_seconds = max(1, min(10, wait_seconds))

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )

            # Route guard: the browser follows redirects and loads subresources
            # on its own, so every request is re-validated against the blocklist.
            # DNS verdicts are cached per hostname for the page's lifetime.
            dns_verdicts: dict[str, str | None] = {}

            async def _guard(route):
                host = urlparse(route.request.url).hostname or ""
                if host in dns_verdicts:
                    guard_err = dns_verdicts[host]
                else:
                    # getaddrinfo is blocking — keep it off the event loop
                    guard_err, _ = await asyncio.to_thread(_validate_url, route.request.url)
                    dns_verdicts[host] = guard_err
                if guard_err:
                    await route.abort("blockedbyclient")
                else:
                    await route.continue_()

            await page.route("**/*", _guard)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(wait_seconds * 1000)

            # Extract readable text content
            text = await page.evaluate("""() => {
                // Remove script, style, nav, footer, header noise
                for (const el of document.querySelectorAll(
                    'script, style, noscript, nav, footer, header, iframe, svg'
                )) { el.remove(); }
                return document.body ? document.body.innerText : document.documentElement.innerText;
            }""")

            title = await page.title()
            await browser.close()

        text = text.strip()
        if not text:
            return f"Page loaded but no text content extracted from: {url}"

        if len(text) > max_length:
            text = text[:max_length] + "\n\n[truncated]"

        return f"**{title}**\n\n{text}" if title else text

    except Exception as e:
        return f"Browse error: {e}"


@tool(
    name="install_mcp",
    description="Connect a new MCP server — its tools become available to all agents automatically",
    category="system",
    hitl=True,
)
async def install_mcp(
    ctx: ToolContext,
    name: str,
    transport: str = "sse",
    url: str = "",
    command: str = "",
    args: str = "",
    cwd: str = "",
    headers: str = "",
    description: str = "",
) -> str:
    """Add an MCP server. Writes to mcp.yaml and regenerates .mcp.json for all agents.

    For remote servers (sse):
        install_mcp(name="acme", url="https://mcp.example.com/mcp")
        install_mcp(name="acme", url="https://...", headers="Authorization: Bearer token123")

    For local servers (stdio):
        install_mcp(name="fs", transport="stdio", command="npx",
                    args="@modelcontextprotocol/server-filesystem /home/docs")

    Args:
        name: Server name (e.g. 'acme', 'google-workspace')
        transport: 'sse' for remote, 'stdio' for local subprocess
        url: Server URL (required for sse)
        command: Executable path (required for stdio)
        args: Space-separated arguments for stdio command
        cwd: Working directory for stdio server
        headers: HTTP headers as 'Key: Value' lines (one per line, for sse auth)
        description: Human-readable description of the server
    """
    from src.core.digest import ingest_mcp_server

    # Parse headers from "Key: Value" lines
    parsed_headers = {}
    if headers:
        for line in headers.strip().splitlines():
            if ": " in line:
                k, v = line.split(": ", 1)
                parsed_headers[k.strip()] = v.strip()

    # Parse args from space-separated string
    parsed_args = args.split() if args else None

    try:
        ingest_mcp_server(
            name,
            transport=transport,
            url=url,
            command=command,
            args=parsed_args,
            cwd=cwd,
            headers=parsed_headers or None,
            description=description,
        )
    except ValueError as e:
        return f"Error: {e}"

    result = f"MCP server **{name}** installed ({transport}).\n"
    if url:
        result += f"URL: {url}\n"
    if command:
        result += f"Command: {command} {args}\n"
    if parsed_headers:
        result += f"Auth: {len(parsed_headers)} header(s) configured\n"

    result += "\n.mcp.json regenerated for all agents. Restart agents to connect."
    return result


@tool(
    name="list_capabilities",
    description="List all available tools, skills, and MCP servers",
    category="system",
)
async def list_capabilities(ctx: ToolContext) -> str:
    """Show everything the system can do — tools, skills, MCP servers."""
    from src.core.skills import get_all_skills

    lines = []

    # Tools
    tools = get_all_tools()
    lines.append(f"**Tools ({len(tools)}):**")
    by_category: dict[str, list] = {}
    for name, td in sorted(tools.items()):
        by_category.setdefault(td.category, []).append(f"  `{name}` — {td.description}")
    for cat in sorted(by_category):
        lines.append(f"  *{cat}:*")
        lines.extend(by_category[cat])

    # Skills
    skills = get_all_skills()
    if skills:
        lines.append(f"\n**Skills ({len(skills)}):**")
        for name, skill in sorted(skills.items()):
            params = ", ".join(p.name for p in skill.parameters) if skill.parameters else ""
            lines.append(f"  `/{name.replace('_', '-')}` ({params}) — {skill.description}")

    # MCP
    mcp_path = PROJECT_ROOT / "config" / "mcp.yaml"
    if mcp_path.exists():
        import yaml
        with open(mcp_path) as f:
            mcp = yaml.safe_load(f) or {}
        servers = mcp.get("servers", {})
        if servers:
            lines.append(f"\n**MCP Servers ({len(servers)}):**")
            for name, cfg in servers.items():
                lines.append(f"  `{name}` — {cfg.get('url', 'N/A')} ({cfg.get('transport', 'sse')})")

    return "\n".join(lines)
