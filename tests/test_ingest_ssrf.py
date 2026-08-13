"""SSRF protections in src/tools/ingest.py — blocklist, redirect re-validation, IP pinning.

All DNS and HTTP is mocked; no network access.
"""

import ipaddress
import socket
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from src.tools.ingest import _safe_urlopen, _validate_url

PUBLIC_IP = "93.184.216.34"


def _fake_getaddrinfo(table: dict[str, str]):
    """A getaddrinfo replacement resolving hostnames via a fixed table."""
    def fake(host, *args, **kwargs):
        ip = table.get(host, host)  # IP literals resolve to themselves
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, 0, 0, 0) if family == socket.AF_INET6 else (ip, 0)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]
    return fake


# --- _validate_url blocklist ---

def test_blocks_link_local_metadata_ip():
    err, ip = _validate_url("http://169.254.169.254/latest/meta-data/")
    assert err and "internal address" in err
    assert ip is None


def test_blocks_ipv4_mapped_ipv6(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _fake_getaddrinfo({"evil.example": "::ffff:169.254.169.254"}),
    )
    err, ip = _validate_url("http://evil.example/")
    assert err and "internal address" in err
    assert ip is None


def test_allows_ipv4_mapped_public_ipv6(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _fake_getaddrinfo({"ok.example": f"::ffff:{PUBLIC_IP}"}),
    )
    err, ip = _validate_url("http://ok.example/")
    assert err is None
    # Compare parsed addresses rather than their text. CPython renders an
    # IPv4-mapped IPv6 address as '::ffff:93.184.216.34' from 3.13 on and as
    # '::ffff:5db8:d822' before it, so asserting on the string pins the test to
    # one interpreter — it passed locally and failed in CI for that reason alone.
    assert ipaddress.ip_address(ip) == ipaddress.ip_address(f"::ffff:{PUBLIC_IP}")


def test_allows_public_hostname(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"ok.example": PUBLIC_IP}),
    )
    err, ip = _validate_url("https://ok.example/page")
    assert err is None
    assert ip == PUBLIC_IP


# --- _safe_urlopen: manual redirect validation ---

class _FakeOpener:
    """Stands in for the pinned-IP opener; scripted responses per call."""

    def __init__(self, script):
        self.script = list(script)
        self.opened: list[str] = []

    def open(self, req, timeout=None):
        self.opened.append(req.full_url)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _redirect(url: str, location: str, code: int = 302) -> HTTPError:
    headers = Message()
    headers["Location"] = location
    return HTTPError(url, code, "Found", headers, None)


def _install_opener(monkeypatch, opener):
    monkeypatch.setattr("src.tools.ingest.build_opener", lambda *handlers: opener)


def test_safe_urlopen_refuses_redirect_to_internal(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"ok.example": PUBLIC_IP}),
    )
    opener = _FakeOpener([_redirect("http://ok.example/", "http://127.0.0.1/secret")])
    _install_opener(monkeypatch, opener)

    with pytest.raises(URLError, match="internal address"):
        _safe_urlopen("http://ok.example/", timeout=5)
    assert opener.opened == ["http://ok.example/"]  # never fetched the redirect target


def test_safe_urlopen_follows_safe_redirect(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _fake_getaddrinfo({"ok.example": PUBLIC_IP, "other.example": PUBLIC_IP}),
    )
    sentinel = object()
    opener = _FakeOpener([
        _redirect("http://ok.example/", "https://other.example/final", code=301),
        sentinel,
    ])
    _install_opener(monkeypatch, opener)

    resp = _safe_urlopen("http://ok.example/", timeout=5)
    assert resp is sentinel
    assert opener.opened == ["http://ok.example/", "https://other.example/final"]


def test_safe_urlopen_resolves_relative_location(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"ok.example": PUBLIC_IP}),
    )
    sentinel = object()
    opener = _FakeOpener([_redirect("http://ok.example/a", "/b"), sentinel])
    _install_opener(monkeypatch, opener)

    assert _safe_urlopen("http://ok.example/a", timeout=5) is sentinel
    assert opener.opened == ["http://ok.example/a", "http://ok.example/b"]


def test_safe_urlopen_refuses_too_many_redirects(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"ok.example": PUBLIC_IP}),
    )
    opener = _FakeOpener(
        [_redirect(f"http://ok.example/{i}", f"http://ok.example/{i + 1}") for i in range(10)]
    )
    _install_opener(monkeypatch, opener)

    with pytest.raises(URLError, match="Too many redirects"):
        _safe_urlopen("http://ok.example/0", timeout=5, max_redirects=3)
    assert len(opener.opened) == 4  # initial request + 3 followed redirects


def test_safe_urlopen_blocks_initial_internal_url():
    with pytest.raises(URLError, match="internal address"):
        _safe_urlopen("http://127.0.0.1/", timeout=5)


def test_safe_urlopen_passes_through_http_errors(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo({"ok.example": PUBLIC_IP}),
    )
    opener = _FakeOpener([HTTPError("http://ok.example/", 404, "Not Found", Message(), None)])
    _install_opener(monkeypatch, opener)

    with pytest.raises(HTTPError) as exc_info:
        _safe_urlopen("http://ok.example/", timeout=5)
    assert exc_info.value.code == 404
