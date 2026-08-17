"""Shared SSRF guard — one blocklist and one validator for every outbound fetch.

Previously http_request, browser, chrome_desktop and ingest each carried their
own copy of the blocked-network list; they drifted (some missed 0.0.0.0/8,
CGNAT, TEST-NET). This module is the single source of truth. ingest keeps its
own IP-pinned connection classes but imports BLOCKED_NETS from here.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

# Internal / non-routable / metadata ranges an agent must never reach.
BLOCKED_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),        # "this host" — 0.0.0.0 hits local services on Linux
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("10.0.0.0/8"),       # private
    ipaddress.ip_network("172.16.0.0/12"),    # private
    ipaddress.ip_network("192.168.0.0/16"),   # private
    ipaddress.ip_network("169.254.0.0/16"),   # link-local (AWS/GCP metadata 169.254.169.254)
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT
    ipaddress.ip_network("192.0.0.0/24"),     # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),    # benchmarking
    ipaddress.ip_network("::1/128"),          # loopback (v6)
    ipaddress.ip_network("fc00::/7"),         # unique-local (v6)
    ipaddress.ip_network("fe80::/10"),        # link-local (v6)
]


def ip_is_blocked(ip: "ipaddress._BaseAddress") -> bool:
    """True if ip (or its embedded IPv4, for ::ffff: mapped addresses) is blocked."""
    mapped = getattr(ip, "ipv4_mapped", None)
    candidates = [ip] + ([mapped] if mapped else [])
    return any(c in net for c in candidates for net in BLOCKED_NETS)


def validate_url(url: str) -> str | None:
    """Block non-http(s) and internal/private URLs. Returns an error string or None."""
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
        if ip_is_blocked(ip):
            return f"Blocked: {host} resolves to internal address {ip}."
    return None


async def install_playwright_guard(page) -> None:
    """Re-validate every request a page makes (redirects, subresources, JS nav,
    clicks) against the blocklist — not just the initial URL. Without this a page
    can redirect or link to http://169.254.169.254/ or a loopback service and the
    response comes back in the transcript. DNS verdicts are cached per host.
    """
    verdicts: dict[str, str | None] = {}

    async def _guard(route):
        host = urlparse(route.request.url).hostname or ""
        if host not in verdicts:
            # getaddrinfo is blocking — keep it off the event loop.
            verdicts[host] = await asyncio.to_thread(validate_url, route.request.url)
        if verdicts[host]:
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    await page.route("**/*", _guard)
