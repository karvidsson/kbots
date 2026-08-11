"""OAuth2 token refresh module.

Handles Google OAuth2 token refresh for Gmail, Drive, Calendar, etc.
Tokens stored in vault (encrypted). Auto-refreshes when expired.
"""

import asyncio
import json
import logging
import time

import aiohttp

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class OAuth2Token:
    """Manages an OAuth2 access token with auto-refresh."""

    def __init__(self, vault, key_prefix: str, *,
                 client_id_key: str | None = None,
                 client_secret_key: str | None = None,
                 refresh_token_key: str | None = None):
        """
        Args:
            vault: VaultBackend instance
            key_prefix: Display name for error messages
            client_id_key: Vault key for client ID (default: {prefix}_CLIENT_ID)
            client_secret_key: Vault key for client secret (default: {prefix}_CLIENT_SECRET)
            refresh_token_key: Vault key for refresh token (default: {prefix}_REFRESH_TOKEN)
        """
        self.vault = vault
        self.prefix = key_prefix
        self._client_id_key = client_id_key or f"{key_prefix}_CLIENT_ID"
        self._client_secret_key = client_secret_key or f"{key_prefix}_CLIENT_SECRET"
        self._refresh_token_key = refresh_token_key or f"{key_prefix}_REFRESH_TOKEN"
        self._access_token: str | None = None
        self._expires_at: float = 0
        self._token_url: str = GOOGLE_TOKEN_URL
        self._refresh_lock = asyncio.Lock()

    @property
    def client_id(self) -> str:
        return self.vault.get(self._client_id_key) or ""

    @property
    def client_secret(self) -> str:
        return self.vault.get(self._client_secret_key) or ""

    @property
    def refresh_token(self) -> str:
        return self.vault.get(self._refresh_token_key) or ""

    async def get_token(self) -> str:
        """Get a valid access token, refreshing if expired."""
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token

        async with self._refresh_lock:
            # Double-check after acquiring lock (another coroutine may have refreshed)
            if self._access_token and time.time() < self._expires_at - 60:
                return self._access_token
            return await self._refresh()

    async def _refresh(self) -> str:
        """Refresh the access token using the refresh token."""
        if not self.client_id or not self.client_secret or not self.refresh_token:
            raise ValueError(
                f"Missing OAuth2 credentials for {self.prefix}. "
                f"Need: {self._client_id_key}, {self._client_secret_key}, "
                f"{self._refresh_token_key} in vault."
            )

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._token_url, data=data, timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    result = await resp.json()
                    if resp.status != 200:
                        error = result.get("error_description", result.get("error", resp.status))
                        raise RuntimeError(f"OAuth2 refresh failed for {self.prefix}: {error}")
        except aiohttp.ClientError as e:
            logger.error(f"OAuth2 refresh failed for {self.prefix}: {e}")
            raise

        self._access_token = result["access_token"]
        self._expires_at = time.time() + result.get("expires_in", 3600)
        logger.info(f"OAuth2 token refreshed for {self.prefix} (expires in {result.get('expires_in', '?')}s)")

        # Store new refresh token if provided (rotation)
        if "refresh_token" in result:
            self.vault.set(self._refresh_token_key, result["refresh_token"])

        return self._access_token


class GoogleAuth:
    """Google API authentication manager. One token per scope/service.

    Reads credentials from vault key 'secrets/google-api-credentials.json'
    (JSON with installed.client_id, installed.client_secret, refresh_token).
    Falls back to GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN.

    Named accounts (account="pixel-fox") let one install talk to several
    Google identities: the refresh token is read STRICTLY from
    GOOGLE_REFRESH_TOKEN__<account> — no fallback to the default account's
    token, so a missing consent fails loudly instead of silently acting as
    someone else's mailbox. The OAuth client (id/secret) falls back to the
    default keys, since one Google Cloud client normally serves every
    account's consent flow.
    """

    def __init__(self, vault, account: str = ""):
        self.vault = vault
        self.account = account
        # Always extract the default blob first: named accounts fall back to
        # the shared client keys, which only exist after this runs — on a
        # fresh install nothing else has extracted them yet.
        self._extract_google_creds(vault)
        if account:
            self._extract_google_creds(vault, account)
        if account:
            suffix = f"__{account}"
            client_id_key = (f"GOOGLE_CLIENT_ID{suffix}"
                             if vault.get(f"GOOGLE_CLIENT_ID{suffix}")
                             else "GOOGLE_CLIENT_ID")
            client_secret_key = (f"GOOGLE_CLIENT_SECRET{suffix}"
                                 if vault.get(f"GOOGLE_CLIENT_SECRET{suffix}")
                                 else "GOOGLE_CLIENT_SECRET")
            refresh_token_key = f"GOOGLE_REFRESH_TOKEN{suffix}"
            prefix = f"GOOGLE[{account}]"
        else:
            client_id_key = "GOOGLE_CLIENT_ID"
            client_secret_key = "GOOGLE_CLIENT_SECRET"
            refresh_token_key = "GOOGLE_REFRESH_TOKEN"
            prefix = "GOOGLE"
        self._token = OAuth2Token(
            vault, prefix,
            client_id_key=client_id_key,
            client_secret_key=client_secret_key,
            refresh_token_key=refresh_token_key,
        )

    @staticmethod
    def _extract_google_creds(vault, account: str = "") -> None:
        """Extract Google creds from JSON blob into individual vault keys if needed."""
        suffix = f"__{account}" if account else ""
        if vault.get(f"GOOGLE_CLIENT_ID{suffix}") and (
                not account or vault.get(f"GOOGLE_REFRESH_TOKEN{suffix}")):
            return  # Already extracted

        creds_json = vault.get(f"secrets/google-api-credentials{suffix}.json")
        if not creds_json:
            return

        try:
            creds = json.loads(creds_json) if isinstance(creds_json, str) else creds_json
            installed = creds.get("installed", creds)
            if installed.get("client_id"):
                vault.set(f"GOOGLE_CLIENT_ID{suffix}", installed["client_id"])
            if installed.get("client_secret"):
                vault.set(f"GOOGLE_CLIENT_SECRET{suffix}", installed["client_secret"])
            if installed.get("refresh_token"):
                vault.set(f"GOOGLE_REFRESH_TOKEN{suffix}", installed["refresh_token"])
            elif creds.get("refresh_token"):
                vault.set(f"GOOGLE_REFRESH_TOKEN{suffix}", creds["refresh_token"])
            logger.info(f"Google credentials extracted from JSON blob"
                        f"{f' for account {account}' if account else ''}")
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error(f"Failed to parse Google credentials JSON: {e}")

    async def get_headers(self) -> dict:
        """Get Authorization headers for Google API calls."""
        token = await self._token.get_token()
        return {"Authorization": f"Bearer {token}"}
