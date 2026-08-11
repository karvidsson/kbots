"""kbots CLI — vault, run, and manage.

Usage:
    python -m src.cli vault init
    python -m src.cli run
    python -m src.cli healthcheck
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("kbots-cli")

SCRIPT_DIR = Path(__file__).parent.parent  # project root


# ============================================================
# VAULT commands
# ============================================================

def cmd_vault(args):
    """Vault management commands."""
    if args.vault_cmd == "init":
        vault_init(args)
    else:
        logger.error(f"Unknown vault command: {args.vault_cmd}")


def vault_init(args):
    """Create a new encrypted vault from .env secrets."""
    env_file = SCRIPT_DIR / ".env"
    vault_file = SCRIPT_DIR / "config" / "secrets.enc"

    if vault_file.exists():
        logger.error(f"Vault already exists at {vault_file}")
        logger.info("To re-create, delete it first: rm config/secrets.enc")
        sys.exit(1)

    # Read secrets from .env
    secrets = {}
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    if value:
                        secrets[key.strip()] = value.strip()

    if not secrets:
        logger.info("No secrets found in .env — creating empty vault")

    # Get passphrase
    import getpass
    passphrase = getpass.getpass("  Vault passphrase: ")
    confirm = getpass.getpass("  Confirm passphrase: ")
    if passphrase != confirm:
        logger.error("Passphrases don't match")
        sys.exit(1)

    # Encrypt
    import base64
    import hashlib

    from cryptography.fernet import Fernet

    # Derive key from passphrase
    key = base64.urlsafe_b64encode(hashlib.pbkdf2_hmac(
        # NOTE: legacy salt — never rename; changing it would break unlocking
        # every existing vault (pre-dates the kagents→kbots rename).
        "sha256", passphrase.encode(), b"kagents-vault-salt", 100_000, dklen=32
    ))
    fernet = Fernet(key)

    payload = json.dumps(secrets).encode()
    encrypted = fernet.encrypt(payload)

    vault_file.parent.mkdir(parents=True, exist_ok=True)
    with open(vault_file, "wb") as f:
        f.write(encrypted)
    vault_file.chmod(0o600)

    logger.info(f"  [ok] Vault created: {vault_file} ({len(secrets)} secrets)")
    logger.info("  Passphrase is NOT stored anywhere. Don't lose it.")


# ============================================================
# RUN
# ============================================================

def cmd_run(args):
    """Run kbots."""
    import asyncio

    from src.main import main
    asyncio.run(main())


# ============================================================
# HEALTHCHECK
# ============================================================

def cmd_healthcheck(args):
    """Check that all configured services are reachable."""
    config_file = SCRIPT_DIR / "config" / "config.yaml"
    if not config_file.exists():
        logger.error("No config found. Run setup.sh first.")
        sys.exit(1)

    with open(config_file) as f:
        config = yaml.safe_load(f)

    if config is None:
        logger.error("Config file is empty.")
        sys.exit(1)

    # Check inline memory backend
    mem_db = Path(SCRIPT_DIR / "data" / "memory.db")
    if mem_db.exists():
        size_mb = mem_db.stat().st_size / (1024 * 1024)
        logger.info(f"  [ok] memory (SQLite): {size_mb:.1f}MB ({mem_db})")
    else:
        logger.info(f"  [--] memory (SQLite): not found at {mem_db}")

    # Check embedding model
    model_dir = Path(SCRIPT_DIR / "data" / "models" / "bge-small-en-v1.5")
    if model_dir.exists():
        logger.info(f"  [ok] embedding model: {model_dir}")
    else:
        logger.info("  [--] embedding model: not found (will download on first use)")

    # Check vault
    vault_file = SCRIPT_DIR / "config" / "secrets.enc"
    if vault_file.exists():
        logger.info(f"  [ok] Vault: {vault_file} ({vault_file.stat().st_size} bytes)")
    else:
        logger.info("  [--] Vault: not created (using .env)")

    # Check .env
    env_file = SCRIPT_DIR / ".env"
    if env_file.exists():
        with open(env_file) as f:
            keys = [line.split("=")[0] for line in f if line.strip() and not line.startswith("#") and "=" in line]
        logger.info(f"  [ok] .env: {len(keys)} keys ({', '.join(keys)})")
    else:
        logger.info("  [--] .env: not found")


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        prog="kbots",
        description="kbots — Lightweight Agent System",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # vault
    p_vault = subparsers.add_parser("vault", help="Vault management")
    vault_sub = p_vault.add_subparsers(dest="vault_cmd", required=True)
    vault_sub.add_parser("init", help="Create vault from .env")
    p_vault.set_defaults(func=cmd_vault)

    # run
    p_run = subparsers.add_parser("run", help="Run kbots")
    p_run.set_defaults(func=cmd_run)

    # healthcheck
    p_health = subparsers.add_parser("healthcheck", help="Check service health")
    p_health.set_defaults(func=cmd_healthcheck)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
