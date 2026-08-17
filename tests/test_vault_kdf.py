"""Vault KDF hardening — stronger iterations without breaking existing vaults.

The critical property: a vault created before this change (legacy shared salt,
100k iterations, no .kdf sidecar) must still unlock, and a rekey must upgrade it
to a per-instance salt + strong iterations while preserving every secret.
"""

import base64
import hashlib
import json

from cryptography.fernet import Fernet

from src.vault.fernet import LEGACY_ITERATIONS, LEGACY_SALT, PBKDF2_ITERATIONS, FernetVault


def _write_legacy_vault(path, secrets, passphrase):
    """Hand-craft a vault exactly as the old inline cli path did: legacy shared
    salt, 100k iterations, no .salt and no .kdf sidecar."""
    key = base64.urlsafe_b64encode(hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode(), LEGACY_SALT, LEGACY_ITERATIONS, dklen=32))
    enc = Fernet(key).encrypt(json.dumps(secrets).encode())
    path.write_bytes(enc)


def test_legacy_vault_still_unlocks(tmp_path):
    vf = tmp_path / "secrets.enc"
    _write_legacy_vault(vf, {"github-token": "ghp_legacy"}, "hunter2")
    v = FernetVault(vault_path=str(vf))
    v.unlock("hunter2")
    assert v.get("github-token") == "ghp_legacy"


def test_wrong_passphrase_still_rejected(tmp_path):
    import pytest
    vf = tmp_path / "secrets.enc"
    _write_legacy_vault(vf, {"k": "v"}, "right")
    v = FernetVault(vault_path=str(vf))
    with pytest.raises(ValueError):
        v.unlock("wrong")


def test_fresh_vault_uses_strong_iterations(tmp_path):
    vf = tmp_path / "secrets.enc"
    v = FernetVault(vault_path=str(vf))
    v.unlock("pw")            # fresh vault → should record strong iterations
    v.set("a", "b")
    kdf = json.loads((tmp_path / "secrets.kdf").read_text())
    assert kdf["iterations"] == PBKDF2_ITERATIONS
    assert (tmp_path / "secrets.salt").exists()
    # reopen with a new instance — must decrypt at the recorded iterations
    v2 = FernetVault(vault_path=str(vf))
    v2.unlock("pw")
    assert v2.get("a") == "b"


def test_rekey_upgrades_and_preserves_secrets(tmp_path):
    vf = tmp_path / "secrets.enc"
    _write_legacy_vault(vf, {"one": "1", "two": "2"}, "pw")
    v = FernetVault(vault_path=str(vf))
    v.unlock("pw")
    assert v._load_iterations() == LEGACY_ITERATIONS  # legacy before rekey

    changed = v.rekey("pw")
    assert "iterations" in changed and "salt" in changed
    assert (tmp_path / "secrets.salt").exists()
    assert json.loads((tmp_path / "secrets.kdf").read_text())["iterations"] == PBKDF2_ITERATIONS

    # A fresh instance must unlock the rekeyed vault and see every secret.
    v2 = FernetVault(vault_path=str(vf))
    v2.unlock("pw")
    assert v2.get("one") == "1" and v2.get("two") == "2"


def test_rekey_is_idempotent(tmp_path):
    vf = tmp_path / "secrets.enc"
    v = FernetVault(vault_path=str(vf))
    v.unlock("pw")
    v.set("k", "v")           # fresh → already strong
    assert v.rekey("pw") == {}  # nothing to change
    v2 = FernetVault(vault_path=str(vf))
    v2.unlock("pw")
    assert v2.get("k") == "v"


def test_salt_migration_preserves_iterations(tmp_path):
    """migrate_salt stays a pure salt migration — iterations unchanged."""
    vf = tmp_path / "secrets.enc"
    _write_legacy_vault(vf, {"k": "v"}, "pw")
    v = FernetVault(vault_path=str(vf))
    v.unlock("pw")
    v.migrate_salt("pw")
    assert (tmp_path / "secrets.salt").exists()
    v2 = FernetVault(vault_path=str(vf))
    v2.unlock("pw")           # still 100k (no .kdf written), random salt
    assert v2.get("k") == "v"
    assert v2._load_iterations() == LEGACY_ITERATIONS
