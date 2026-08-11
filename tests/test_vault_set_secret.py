"""vault_set_secret — key allow-list, persistence, and log redaction."""

from src.core.base import ToolContext
from src.core.hitl import _redact_args
from src.tools.agents_admin import vault_set_secret


class _FakeVault:
    def __init__(self):
        self.store = {}

    def set(self, k, v):
        self.store[k] = v

    def get(self, k):
        return self.store.get(k)


async def test_stores_discord_key():
    v = _FakeVault()
    out = await vault_set_secret(ToolContext(agent_id="atlas", vault=v), "discord-scout", "tok-abc-123")
    assert v.store["discord-scout"] == "tok-abc-123"
    assert "Stored 'discord-scout'" in out
    assert "tok-abc-123" not in out          # never echoes the value


async def test_stores_secrets_key():
    v = _FakeVault()
    await vault_set_secret(ToolContext(agent_id="atlas", vault=v), "secrets/tavily-api-key", "tvly-xyz")
    assert v.store["secrets/tavily-api-key"] == "tvly-xyz"


async def test_rejects_disallowed_keys():
    v = _FakeVault()
    ctx = ToolContext(agent_id="atlas", vault=v)
    for bad in ("github-token", "active-x", "../etc/passwd", "randomkey"):
        out = await vault_set_secret(ctx, bad, "value")
        assert "Refused" in out
    assert v.store == {}                       # nothing written


async def test_rejects_empty_value():
    v = _FakeVault()
    out = await vault_set_secret(ToolContext(agent_id="atlas", vault=v), "discord-x", "   ")
    assert "Refused" in out and not v.store


def test_value_is_redacted_in_logs():
    # the 'secret' (and 'key') arg names trigger redaction in the HITL/audit path
    redacted = _redact_args({"key": "discord-scout", "secret": "tok-abc-123"})
    assert "tok-abc-123" not in redacted
    assert "[REDACTED]" in redacted
