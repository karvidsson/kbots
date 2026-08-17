"""Generic HTTP tool — call any REST API without a dedicated tool.

One tool, http_request: GET/POST/PUT/PATCH/DELETE with custom headers, query
params, JSON bodies, and vault-stored auth. SSRF-guarded (blocks private and
link-local addresses). This is the universal glue — reach for it before writing
a service-specific tool.
"""

import json
import logging
from urllib.parse import urljoin, urlparse

from src.core.base import ToolContext
from src.core.tools import tool
from src.lib.ssrf import validate_url as _validate_url

logger = logging.getLogger(__name__)

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
_MAX_BODY = 8000
_MAX_READ = 4 * 1024 * 1024  # 4 MB hard cap on the response we pull into memory


def _host_allowed(host: str, allowed: str) -> bool:
    """True if host matches one of the comma/space-separated allowed suffixes.

    'api.github.com' matches an allow entry of 'github.com' or 'api.github.com'.
    """
    host = (host or "").lower().rstrip(".")
    for entry in allowed.replace(",", " ").split():
        e = entry.strip().lower().lstrip("*.").rstrip(".")
        if e and (host == e or host.endswith("." + e)):
            return True
    return False


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
    A secret can be pinned to specific hosts by storing secrets/<key>.hosts
    (e.g. "api.github.com"); it is then refused for any other host.
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
    auth_header_names: set[str] = set()
    origin_host = (urlparse(url).hostname or "").lower()
    if auth_secret:
        header_name = "Authorization"
        vault_key = auth_secret
        prefix = "Bearer "
        if auth_secret.startswith("header:") and "=" in auth_secret:
            spec, vault_key = auth_secret[len("header:"):].split("=", 1)
            header_name = spec
            prefix = ""
        secret = None
        allowed_hosts = None
        if ctx.vault:
            secret = ctx.vault.get(f"secrets/{vault_key}") or ctx.vault.get(vault_key)
            # Optional host binding: store secrets/<key>.hosts to pin a secret to
            # specific hosts. Prevents an agent from exfiltrating a vault secret
            # to an arbitrary URL (auth_secret + attacker URL was a one-call vault
            # dump). Enforced when present; when absent we allow but warn.
            allowed_hosts = (ctx.vault.get(f"secrets/{vault_key}.hosts")
                             or ctx.vault.get(f"{vault_key}.hosts"))
        if not secret:
            return f"No vault secret found for '{vault_key}'. Add it as secrets/{vault_key}."
        if allowed_hosts:
            if not _host_allowed(origin_host, allowed_hosts):
                return (f"Refusing to send secret '{vault_key}' to '{origin_host}' — "
                        f"it is bound to hosts [{allowed_hosts}]. Update secrets/{vault_key}.hosts "
                        "if this host is legitimate.")
        else:
            logger.warning(
                f"http_request: sending vault secret '{vault_key}' to '{origin_host}' with no "
                f"host binding. Add secrets/{vault_key}.hosts to restrict where it can be sent."
            )
        hdrs[header_name] = f"{prefix}{secret}"
        auth_header_names.add(header_name.lower())

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
                        # Drop auth headers when redirected to a different host so
                        # a redirect can't carry a vault secret to another origin.
                        if auth_header_names and (urlparse(cur_url).hostname or "").lower() != origin_host:
                            hdrs = {k: v for k, v in hdrs.items()
                                    if k.lower() not in auth_header_names}
                            auth_header_names = set()
                        if resp.status in (301, 302, 303):
                            cur_method, cur_body = "GET", None  # standard method downgrade
                        query = None  # params only apply to the first request
                        continue
                    ctype = resp.headers.get("Content-Type", "")
                    raw = await resp.content.read(_MAX_READ)  # bounded — don't OOM on a huge body
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
