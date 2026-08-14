"""Fernet vault — encrypted credential storage.

Secrets are encrypted at rest in secrets.enc, decrypted once at startup
into an in-memory dict. Passphrase is never stored.
"""

import base64
import hashlib
import json
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from src.core.base import VaultBackend

logger = logging.getLogger(__name__)

LEGACY_SALT = b"kagents-vault-salt"
PBKDF2_ITERATIONS = 100_000


def _normalise_key(key: str) -> str:
    """Reduce a vault key to a canonical form for fallback lookups.

    The vault is a flat dict — `secrets/` is part of the key string, not a
    namespace or a path. Nothing enforces a convention and the codebase
    genuinely uses three (`secrets/cloudflare-api-token`, `github-token`,
    `CLOUDFLARE_API_TOKEN`), so an operator cannot infer the right name from
    the ones they can see. They save `cloudflare-api-token`, every Cloudflare
    tool reports "not configured", and the value is sitting in the vault the
    whole time under a near-identical name — in a store you cannot easily
    inspect. That cost two agents ~20 minutes each before it was diagnosed.

    Normalising on *lookup* rather than on save means existing vaults are
    fixed without being rewritten, and no secret is ever re-encrypted or moved
    to repair a name.
    """
    k = key.strip()
    if k.casefold().startswith("secrets/"):
        k = k[len("secrets/"):]
    return k.casefold().replace("_", "-")


# How many bad characters to name individually before summarising the rest. A
# token with one stray character wants that character pinpointed; a value that
# is mostly non-ASCII wants a count, not a transcript of itself in the logs.
_MAX_REPORTED_CHARS = 3


def describe_value_faults(value: str) -> list[str]:
    """Faults that make a stored secret unusable, described without leaking it.

    A correctly-named key can still hold a value that cannot work. One real
    case: a Cloudflare token containing U+0442 CYRILLIC SMALL LETTER TE. HTTP
    headers must be latin-1, so the request could not even be constructed — the
    tool died with "'latin-1' codec can't encode character 'т' in position 12"
    four calls away from anything mentioning the vault. The credential was
    present, correctly named, and found; it was simply unusable.

    Reported faults are limited to the shape of the value — length, positions,
    codepoint names — which identifies the problem while revealing essentially
    nothing about the secret. That is the point: this has to be safe to log and
    safe to print, or it will not be run when it is needed.

    Warnings, never refusals. The vault is a general-purpose store; some keys
    legitimately hold JSON credential blobs that may contain non-ASCII, and
    locking an operator out of saving a value is worse than telling them it
    looks wrong.
    """
    import unicodedata

    faults: list[str] = []

    if value != value.strip():
        where = []
        if value != value.lstrip():
            where.append("leading")
        if value != value.rstrip():
            where.append("trailing")
        # The classic copy-paste newline. Silent, and it surfaces as a 401 that
        # reads like a permissions problem rather than a formatting one.
        faults.append(f"{' and '.join(where)} whitespace "
                      f"(length {len(value)}, {len(value.strip())} once stripped)")

    non_ascii = [(i, c) for i, c in enumerate(value) if ord(c) > 127]
    if non_ascii:
        shown = ", ".join(
            f"index {i}: {hex(ord(c))} {unicodedata.name(c, 'UNNAMED')}"
            for i, c in non_ascii[:_MAX_REPORTED_CHARS])
        more = len(non_ascii) - _MAX_REPORTED_CHARS
        if more > 0:
            shown += f", and {more} more"
        # No API token, account id or bot token legitimately contains one. It is
        # a typed-not-pasted tell, or a homoglyph substitution, and HTTP headers
        # cannot carry it at all.
        faults.append(f"{len(non_ascii)} non-ASCII character(s) — {shown}")

    return faults


class FernetVault(VaultBackend):
    """Fernet-encrypted credential vault."""
    name = "fernet"

    def __init__(self, vault_path: str | Path | None = None):
        if vault_path is None:
            from src.core.base import resolve_config_file
            vault_path = resolve_config_file("secrets.enc")
        self._vault_path = Path(vault_path)
        self._salt_path = self._vault_path.with_suffix(".salt")
        self._secrets: dict[str, str] = {}
        self._fernet: Fernet | None = None
        self._unlocked = False
        # Keys already reported as malformed, so a bad value is flagged once per
        # process instead of on every lookup on a hot path.
        self._warned_faults: set[str] = set()

    @property
    def is_unlocked(self) -> bool:
        return self._unlocked

    def _load_salt(self) -> bytes:
        """Load per-instance salt, or fall back to legacy hardcoded salt.

        The legacy fallback exists only for vaults created before per-instance
        salts; a brand-new vault (no secrets.enc yet) gets a random salt.
        """
        if self._salt_path.exists():
            return self._salt_path.read_bytes()
        if not self._vault_path.exists():
            return self._generate_salt()
        return LEGACY_SALT

    def _generate_salt(self) -> bytes:
        """Generate and persist a random per-instance salt."""
        salt = os.urandom(32)
        self._salt_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._salt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(salt)
        return salt

    def unlock(self, passphrase: str) -> None:
        """Decrypt the vault into memory. Passphrase is not retained."""
        salt = self._load_salt()
        key = self._derive_key(passphrase, salt)
        self._fernet = Fernet(key)

        if self._vault_path.exists():
            encrypted = self._vault_path.read_bytes()
            try:
                decrypted = self._fernet.decrypt(encrypted)
                self._secrets = json.loads(decrypted)
                logger.info(f"Vault unlocked: {len(self._secrets)} secrets loaded")
            except InvalidToken:
                raise ValueError("Wrong passphrase — vault decrypt failed")
            except json.JSONDecodeError:
                raise ValueError("Vault data corrupted — not valid JSON after decryption")
        else:
            self._secrets = {}
            logger.info("Vault file does not exist — starting with empty vault")

        self._unlocked = True

    def unlock_from_env(self) -> None:
        """Load secrets from environment variables instead of vault.

        Useful for development when you don't want to set up a vault.
        Falls back to .env file if present.
        """
        env_file = Path(".env")
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip()
                        if value:
                            self._secrets[key] = value

        # Also load from actual environment
        for key in os.environ:
            if any(key.startswith(prefix) for prefix in (
                "DISCORD_", "ANTHROPIC_", "GROQ_", "OPENAI_",
                "TAVILY_", "SLACK_",
            )):
                self._secrets[key] = os.environ[key]

        self._unlocked = True
        logger.info(f"Vault loaded from environment: {len(self._secrets)} secrets")

    def get(self, key: str) -> str | None:
        """Get a secret by key, preferring an exact match.

        Falls back to a normalised match, so `cloudflare-api-token`,
        `CLOUDFLARE_API_TOKEN` and `secrets/cloudflare-api-token` all resolve to
        the same secret. See `_normalise_key` for why that is worth doing.

        An exact match always wins, so this can only ever turn a miss into a
        hit — it never changes which value an already-working lookup returns.

        The value is also checked for faults on first use, because existing
        vaults already hold malformed secrets that nothing has ever complained
        about.
        """
        value = self._secrets.get(key)
        if value is not None:
            return self._checked(key, value)

        target = _normalise_key(key)
        matches = [k for k in self._secrets if _normalise_key(k) == target]

        if len(matches) == 1:
            logger.info(
                f"Vault: '{key}' not found, resolved to '{matches[0]}' by name "
                f"normalisation. Rename it to '{key}' to silence this.")
            return self._checked(matches[0], self._secrets[matches[0]])

        if len(matches) > 1:
            # Two distinct stored keys normalise the same (e.g. 'github-token'
            # and 'secrets/github-token'). Picking one would risk handing out
            # the wrong credential silently, so refuse and name them both. This
            # is not a regression: the exact lookup already missed, so the
            # caller was getting None either way — now they get a reason.
            logger.warning(
                f"Vault: '{key}' not found, and {len(matches)} stored keys are "
                f"ambiguous matches ({', '.join(sorted(matches))}). Refusing to "
                f"guess — rename one of them to '{key}'.")
            return None

        logger.debug(
            f"Vault: '{key}' not found (vault holds {len(self._secrets)} keys)")
        return None

    def _checked(self, stored_key: str, value: str) -> str:
        """Return `value`, reporting a malformed one the first time it is used.

        Once per key per process: a caller on a hot path should not pay for this
        check repeatedly, and a repeated warning is a warning people filter out.
        """
        if stored_key in self._warned_faults:
            return value
        self._warned_faults.add(stored_key)
        for fault in describe_value_faults(value):
            logger.warning(
                f"Vault: the stored value for '{stored_key}' has {fault}. "
                f"Tools using it will fail somewhere that does not mention the "
                f"vault at all.")
        return value

    def set(self, key: str, value: str) -> None:
        """Set a secret and persist to disk.

        A malformed value is flagged, not rejected — see `describe_value_faults`.
        """
        for fault in describe_value_faults(value):
            logger.warning(f"Vault: value for '{key}' has {fault}. This is "
                           f"stored as given, but it is very unlikely to work.")
        self._secrets[key] = value
        self._warned_faults.discard(key)
        self._persist()

    def delete(self, key: str) -> bool:
        """Delete a secret. Returns True if it existed."""
        if key in self._secrets:
            del self._secrets[key]
            self._persist()
            return True
        return False

    def list_keys(self) -> list[str]:
        """List all secret key names (not values)."""
        return list(self._secrets.keys())

    def _persist(self) -> None:
        """Re-encrypt and write vault to disk."""
        if not self._fernet:
            logger.warning("Cannot persist vault — no encryption key (env-only mode)")
            return

        payload = json.dumps(self._secrets).encode()
        encrypted = self._fernet.encrypt(payload)

        self._vault_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._vault_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(encrypted)

    def migrate_salt(self, passphrase: str) -> None:
        """Migrate from legacy hardcoded salt to per-instance random salt.

        Decrypts with old salt, generates new salt, re-encrypts with new salt.
        Must be called while vault is unlocked.
        """
        if not self._unlocked:
            raise RuntimeError("Vault must be unlocked before migrating salt")
        if self._salt_path.exists():
            raise RuntimeError("Per-instance salt already exists — already migrated")

        # Generate new salt and re-derive key
        new_salt = self._generate_salt()
        new_key = self._derive_key(passphrase, new_salt)
        self._fernet = Fernet(new_key)

        # Re-encrypt with new key
        self._persist()
        logger.info(f"Vault salt migrated to per-instance salt at {self._salt_path}")

    @staticmethod
    def _derive_key(passphrase: str, salt: bytes) -> bytes:
        """Derive a Fernet key from a passphrase using PBKDF2."""
        return base64.urlsafe_b64encode(hashlib.pbkdf2_hmac(
            "sha256", passphrase.encode(), salt, PBKDF2_ITERATIONS, dklen=32
        ))
