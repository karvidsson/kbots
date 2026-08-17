"""Phase-3 network-surface regression tests.

- shared SSRF blocklist covers the previously-missing ranges (0.0.0.0/8, CGNAT,
  TEST-NET) and IPv4-mapped IPv6
- the Playwright route guard aborts requests to internal hosts
- webhook rate limiter caps per-trigger and global fire rate
- webhook-secret values are scrubbed from persisted output
"""

import ipaddress

import pytest

from src.core.audit import scrub_value
from src.lib import ssrf


@pytest.mark.parametrize("ip", [
    "0.0.0.0", "127.0.0.1", "10.1.2.3", "172.16.5.5", "192.168.1.1",
    "169.254.169.254",             # cloud metadata
    "100.64.0.1",                  # CGNAT (was missing)
    "198.18.0.1",                  # TEST-NET benchmarking (was missing)
    "::1",
])
def test_blocked_ranges(ip):
    assert ssrf.ip_is_blocked(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "1.1.1.1"])
def test_public_allowed(ip):
    assert not ssrf.ip_is_blocked(ipaddress.ip_address(ip))


def test_ipv4_mapped_ipv6_blocked():
    # ::ffff:169.254.169.254 must be caught via its embedded IPv4.
    assert ssrf.ip_is_blocked(ipaddress.ip_address("::ffff:169.254.169.254"))


def test_validate_url_scheme_and_private(monkeypatch):
    assert "Blocked scheme" in ssrf.validate_url("file:///etc/passwd")
    monkeypatch.setattr("src.lib.ssrf.socket.getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 0))])
    assert "internal address" in ssrf.validate_url("http://metadata.local/")
    monkeypatch.setattr("src.lib.ssrf.socket.getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert ssrf.validate_url("http://example.com/") is None


async def test_playwright_guard_aborts_internal(monkeypatch):
    monkeypatch.setattr("src.lib.ssrf.socket.getaddrinfo",
                        lambda host, *a, **k: [(2, 1, 6, "", (
                            "169.254.169.254" if "meta" in host else "93.184.216.34", 0))])
    aborted, continued = [], []

    class _Route:
        def __init__(self, url):
            self.request = type("R", (), {"url": url})()

        async def abort(self, reason):
            aborted.append(self.request.url)

        async def continue_(self):
            continued.append(self.request.url)

    captured = {}

    class _Page:
        async def route(self, pattern, handler):
            captured["h"] = handler

    await ssrf.install_playwright_guard(_Page())
    await captured["h"](_Route("http://metadata.internal/latest"))
    await captured["h"](_Route("http://example.com/ok"))
    assert aborted == ["http://metadata.internal/latest"]
    assert continued == ["http://example.com/ok"]


# --- webhook rate limiter ---

def _connector():
    from src.connectors.webhook import WebhookConnector
    return WebhookConnector({"host": "127.0.0.1", "port": 0})


def test_per_trigger_rate_cap():
    from src.connectors import webhook
    c = _connector()
    ok = sum(c._rate_ok("t1") for _ in range(webhook._RATE_PER_TRIGGER_PER_MIN + 10))
    assert ok == webhook._RATE_PER_TRIGGER_PER_MIN
    # a different trigger has its own budget
    assert c._rate_ok("t2") is True


def test_global_rate_cap():
    from src.connectors import webhook
    c = _connector()
    fired = 0
    for i in range(webhook._RATE_GLOBAL_PER_MIN + 50):
        if c._rate_ok(f"trig-{i}"):   # unique trigger each time → only global cap bites
            fired += 1
    assert fired == webhook._RATE_GLOBAL_PER_MIN


# --- webhook secret scrubbing ---

def test_webhook_secret_scrubbed():
    line = "curl -H 'X-Webhook-Secret: kZ3n_abcDEF1234567890xy' https://x/event/t1"
    out = scrub_value(line)
    assert "kZ3n_abcDEF" not in out and "[REDACTED]" in out
