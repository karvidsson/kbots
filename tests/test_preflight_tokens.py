"""Preflight — a missing Discord bot token warns + skips that bot, never aborts boot."""

from src.core import preflight


class _Vault:
    def __init__(self, present):
        self._present = set(present)

    def get(self, k):
        return "x" * 60 if k in self._present else None


def _patch_ok(monkeypatch, storage_ok=True):
    monkeypatch.setattr(preflight, "validate_config", lambda cfg: ([], []))
    monkeypatch.setattr(preflight, "_check_claude_cli", lambda: (True, "ok"))

    async def _auth():
        return (True, "ok")

    monkeypatch.setattr(preflight, "_check_claude_auth", _auth)
    monkeypatch.setattr(preflight, "_check_storage", lambda p: (storage_ok, "ok" if storage_ok else "bad"))
    monkeypatch.setattr(preflight, "_check_agent_paths", lambda a: (True, "ok"))
    monkeypatch.setattr(preflight, "_check_file_ownership", lambda paths: [])


def _cfg(accounts):
    return {"connectors": {"discord": {"enabled": True, "accounts": accounts}}, "agents": {}}


async def test_missing_secondary_token_is_non_fatal(monkeypatch):
    _patch_ok(monkeypatch)
    cfg = _cfg({"main": {"token_key": "discord-token"},
                "scout": {"token_key": "discord-scout"}})   # scout token NOT in vault
    vault = _Vault(present={"discord-token"})
    assert await preflight.run_preflight(cfg, vault, "/tmp/x.db") is True     # boots anyway


async def test_all_tokens_missing_still_boots(monkeypatch):
    _patch_ok(monkeypatch)
    cfg = _cfg({"main": {"token_key": "discord-token"}})
    vault = _Vault(present=set())
    assert await preflight.run_preflight(cfg, vault, "/tmp/x.db") is True


async def test_real_critical_failure_still_aborts(monkeypatch):
    _patch_ok(monkeypatch, storage_ok=False)     # storage remains a hard, boot-blocking check
    cfg = _cfg({"main": {"token_key": "discord-token"}})
    vault = _Vault(present={"discord-token"})
    assert await preflight.run_preflight(cfg, vault, "/tmp/x.db") is False
