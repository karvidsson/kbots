"""GoogleAuth named accounts — per-identity refresh tokens, no silent fallback.

A named account (account="pixel-fox") must never fall back to the default
account's refresh token: that would silently act as another identity's
mailbox. The OAuth client id/secret MAY fall back — one Google Cloud client
serves every account's consent flow.
"""

import json

import pytest

from src.auth.oauth2 import GoogleAuth


class DictVault:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


def test_default_account_uses_legacy_keys():
    vault = DictVault({
        "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "cs",
        "GOOGLE_REFRESH_TOKEN": "rt-default",
    })
    auth = GoogleAuth(vault)
    assert auth._token.client_id == "cid"
    assert auth._token.refresh_token == "rt-default"


def test_named_account_uses_own_refresh_token_shared_client():
    vault = DictVault({
        "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "cs",
        "GOOGLE_REFRESH_TOKEN": "rt-default",
        "GOOGLE_REFRESH_TOKEN__pixel-fox": "rt-husky",
    })
    auth = GoogleAuth(vault, account="pixel-fox")
    assert auth._token.client_id == "cid"          # shared client
    assert auth._token.refresh_token == "rt-husky"  # own identity


def test_named_account_never_falls_back_to_default_token():
    vault = DictVault({
        "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "cs",
        "GOOGLE_REFRESH_TOKEN": "rt-default",
    })
    auth = GoogleAuth(vault, account="pixel-fox")
    assert auth._token.refresh_token == ""  # missing consent = no token, loud failure


async def test_named_account_missing_token_refresh_raises():
    vault = DictVault({
        "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "cs",
        "GOOGLE_REFRESH_TOKEN": "rt-default",
    })
    auth = GoogleAuth(vault, account="pixel-fox")
    with pytest.raises(ValueError, match="GOOGLE\\[pixel-fox\\]"):
        await auth._token._refresh()


def test_named_account_own_client_wins_when_present():
    vault = DictVault({
        "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "cs",
        "GOOGLE_CLIENT_ID__band": "cid-band", "GOOGLE_CLIENT_SECRET__band": "cs-band",
        "GOOGLE_REFRESH_TOKEN__band": "rt-band",
    })
    auth = GoogleAuth(vault, account="band")
    assert auth._token.client_id == "cid-band"
    assert auth._token.refresh_token == "rt-band"


def test_account_creds_blob_extracts_to_suffixed_keys():
    blob = json.dumps({"installed": {
        "client_id": "cid-nh", "client_secret": "cs-nh",
        "refresh_token": "rt-nh",
    }})
    vault = DictVault({"secrets/google-api-credentials__pixel-fox.json": blob})
    auth = GoogleAuth(vault, account="pixel-fox")
    assert vault.get("GOOGLE_CLIENT_ID__pixel-fox") == "cid-nh"
    assert auth._token.refresh_token == "rt-nh"
    # default-account keys untouched
    assert vault.get("GOOGLE_CLIENT_ID") is None


def test_named_account_extracts_shared_client_from_default_blob():
    """Fresh install: only the default creds blob + the account's consent exist."""
    blob = json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}})
    vault = DictVault({
        "secrets/google-api-credentials.json": blob,
        "GOOGLE_REFRESH_TOKEN__pixel-fox": "rt-husky",
    })
    auth = GoogleAuth(vault, account="pixel-fox")
    assert auth._token.client_id == "cid"
    assert auth._token.refresh_token == "rt-husky"
