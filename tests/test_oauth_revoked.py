"""Telling a dead grant apart from a transient failure.

Google answers a revoked or expired refresh token with invalid_grant, and no
retry will ever clear it — only a human re-consenting. Everything else (5xx,
rate limits, network) is worth retrying. Callers that run forever need to know
which they are looking at, so the two raise different exceptions.
"""

import pytest

from src.auth.oauth2 import UNRECOVERABLE_OAUTH_ERRORS, OAuth2AuthRevokedError, OAuth2Token


class DictVault:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response):
        self._response = response

    def post(self, *a, **kw):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _token(monkeypatch, status, payload):
    vault = DictVault({"X_CLIENT_ID": "cid", "X_CLIENT_SECRET": "cs",
                       "X_REFRESH_TOKEN": "rt"})
    tok = OAuth2Token(vault, "X")
    monkeypatch.setattr("src.auth.oauth2.aiohttp.ClientSession",
                        lambda *a, **kw: FakeSession(FakeResponse(status, payload)))
    return tok


async def test_invalid_grant_is_unrecoverable(monkeypatch):
    tok = _token(monkeypatch, 400, {
        "error": "invalid_grant",
        "error_description": "Token has been expired or revoked.",
    })
    with pytest.raises(OAuth2AuthRevokedError) as exc:
        await tok._refresh()
    assert exc.value.error == "invalid_grant"
    assert "revoked" in exc.value.description
    assert "X" in str(exc.value)


async def test_server_error_stays_retryable(monkeypatch):
    """Must NOT be OAuth2AuthRevokedError — a watcher would stop for good on a blip."""
    tok = _token(monkeypatch, 503, {"error": "backend_error",
                                    "error_description": "try again"})
    with pytest.raises(RuntimeError) as exc:
        await tok._refresh()
    assert not isinstance(exc.value, OAuth2AuthRevokedError)


async def test_every_unrecoverable_code_raises_the_dedicated_error(monkeypatch):
    for code in UNRECOVERABLE_OAUTH_ERRORS:
        tok = _token(monkeypatch, 400, {"error": code, "error_description": "nope"})
        with pytest.raises(OAuth2AuthRevokedError):
            await tok._refresh()
