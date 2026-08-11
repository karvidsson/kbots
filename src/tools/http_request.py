"""Generic HTTP tool — call any REST API without a dedicated tool.

One tool, http_request: GET/POST/PUT/PATCH/DELETE with custom headers, query
params, JSON bodies, and vault-stored auth. SSRF-guarded (blocks private and
link-local addresses). This is the universal glue — reach for it before writing
a service-specific tool.
"""

import ipaddress
import json
import logging
import socket
from urllib.parse import urljoin, urlparse

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
_MAX_BODY = 8000

# SSRF protection — block internal networks (same set as browser.py)
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


def _validate_url(url: str) -> str | None:
    """Block non-http(s) and internal/private URLs. Returns error string or None."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Blocked scheme: {parsed.scheme or '(none)'}. Only http/https allowed."
    host = parsed.hostname
    if not host:
        return "No hostname in URL."
    try:
        addrs = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"Cannot resolve hostname: {host}"
    for *_, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        # An IPv4-mapped IPv6 address (::ffff:a.b.c.d) never matches the IPv4
        # nets directly — check the embedded IPv4 as well.
        mapped = getattr(ip, "ipv4_mapped", None)
        candidates = [ip] + ([mapped] if mapped else [])
        for cand in candidates:
            for net in _BLOCKED_NETS:
                if cand in net:
                    return f"Blocked: {host} resolves to internal address {ip}."
    return None


def _parse_json_arg(value: str, label: str):
    """Parse a JSON-string argument. Returns (parsed, None) or (None, error)."""
    if not value or not value.strip():
        return None, None
    try:
        return json.loads(value), None
    except json.JSONDecodeError as e:
        return None, f"{label} is not valid JSON: {e}"


@tool(
    name="http_request",
    description=(
        "Call any HTTP/REST API: GET/POST/PUT/PATCH/DELETE with headers, query params, "
        "a JSON body, and optional vault-stored auth. Use this before writing a "
        "service-specific tool."
    ),
    category="research",
)
async def http_request(ctx: ToolContext, method: str, url: str, headers: str = "",
                       json_body: str = "", params: str = "", auth_secret: str = "",
                       timeout: int = 30) -> str:
    """Make an HTTP request and return status, headers, and body.

    Args:
        method: GET, POST, PUT, PATCH, DELETE, or HEAD.
        url: full http(s) URL.
        headers: optional JSON object string of request headers, e.g. '{"Accept":"application/json"}'.
        json_body: optional JSON string sent as the request body (Content-Type set automatically).
        params: optional JSON object string of query parameters.
        auth_secret: optional vault key name. Its value is sent as 'Authorization: Bearer <value>'.
            Use 'header:X-Api-Key=<vaultkey>' form to send it as a custom header instead.
        timeout: seconds (capped at 120).

    The secret value never appears in the prompt or logs — only the key name.
    """
    method = method.upper().strip()
    if method not in _METHODS:
        return f"Invalid method '{method}'. Valid: {', '.join(_METHODS)}"
    url_err = _validate_url(url)
    if url_err:
        return url_err
    timeout = max(1, min(int(timeout), 120))

    hdrs, err = _parse_json_arg(headers, "headers")
    if err:
        return err
    if hdrs is not None and not isinstance(hdrs, dict):
        return "headers must be a JSON object."
    hdrs = dict(hdrs or {})

    query, err = _parse_json_arg(params, "params")
    if err:
        return err
    if query is not None and not isinstance(query, dict):
        return "params must be a JSON object."

    body, err = _parse_json_arg(json_body, "json_body")
    if err:
        return err

    # Resolve auth from the vault (never inline the secret)
    if auth_secret:
        header_name = "Authorization"
        vault_key = auth_secret
        prefix = "Bearer "
        if auth_secret.startswith("header:") and "=" in auth_secret:
            spec, vault_key = auth_secret[len("header:"):].split("=", 1)
            header_name = spec
            prefix = ""
        secret = None
        if ctx.vault:
            secret = ctx.vault.get(f"secrets/{vault_key}") or ctx.vault.get(vault_key)
        if not secret:
            return f"No vault secret found for '{vault_key}'. Add it as secrets/{vault_key}."
        hdrs[header_name] = f"{prefix}{secret}"

    try:
        import aiohttp
    except ImportError:
        return "aiohttp not installed."

    logger.info(f"http_request {method} {url} (auth={'yes' if auth_secret else 'no'})")
    try:
        async with aiohttp.ClientSession() as session:
            cur_url, cur_method, cur_body = url, method, body
            for _ in range(5):  # follow redirects manually, re-validating each hop
                async with session.request(
                    cur_method, cur_url, headers=hdrs or None, params=query or None,
                    json=cur_body if cur_body is not None else None,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if not loc:
                            return f"{resp.status} {resp.reason} — redirect without a Location."
                        cur_url = urljoin(cur_url, loc)
                        rerr = _validate_url(cur_url)  # block SSRF via redirect
                        if rerr:
                            return rerr
                        if resp.status in (301, 302, 303):
                            cur_method, cur_body = "GET", None  # standard method downgrade
                        query = None  # params only apply to the first request
                        continue
                    ctype = resp.headers.get("Content-Type", "")
                    raw = await resp.read()
                    out = [f"{resp.status} {resp.reason}", f"Content-Type: {ctype}"]
                    if "application/json" in ctype:
                        try:
                            pretty = json.dumps(json.loads(raw.decode()), indent=2)
                            out.append("\n" + pretty[:_MAX_BODY])
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            out.append("\n" + raw.decode(errors="replace")[:_MAX_BODY])
                    elif ctype.startswith(("text/", "application/xml")) or "charset" in ctype:
                        out.append("\n" + raw.decode(errors="replace")[:_MAX_BODY])
                    else:
                        out.append(f"\n[binary body, {len(raw)} bytes, not shown]")
                    if len(raw) > _MAX_BODY:
                        out.append(f"\n… [truncated at {_MAX_BODY} chars]")
                    return "\n".join(out)
            return "Too many redirects (>5)."
    except aiohttp.ClientError as e:
        return f"Request failed: {e}"
    except TimeoutError:
        return f"Request timed out after {timeout}s."
