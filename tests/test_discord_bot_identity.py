"""An agent's Discord calls must use that agent's own bot account.

Regression cover for the 403 "Missing Access" (code 50001) that engineer3 hit
on 2026-09-06 uploading to its own DM: with no explicit bot= the in-process
tool path authenticated as the shared main bot, which is not in that DM. It
was misread as a sandbox egress block. See src/lib/discord_auth.py.
"""

from unittest.mock import MagicMock

import pytest

import src.lib.discord_auth as da
from src.core.base import ToolContext
from src.tools.discord_tools import _discord_headers, send_discord_file


class StubVault:
    """Vault holding only the keys given — a missing key returns None."""

    def __init__(self, secrets):
        self._s = secrets

    def get(self, key):
        return self._s.get(key)


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    """An overlay where engineer3 posts as itself and jarvis as 'main'."""
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "agents.yaml").write_text(
        "agents:\n"
        "  engineer3:\n"
        "    routing:\n"
        "      discord:\n"
        "        account: engineer3\n"
        "  jarvis:\n"
        "    routing:\n"
        "      discord:\n"
        "        account: main\n"
        "  nomad: {}\n"
    )
    (tmp_path / "config" / "config.yaml").write_text(
        "connectors:\n"
        "  discord:\n"
        "    accounts:\n"
        "      main:\n"
        "        token_key: discord-token\n"
        "      engineer3:\n"
        "        token_key: discord-engineer3\n"
    )
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    monkeypatch.delenv("KBOTS_MODULES", raising=False)
    return tmp_path


# --- account and key resolution ---

def test_account_comes_from_routing_not_the_name(overlay):
    assert da.bot_account_for_agent("jarvis") == "main"


def test_account_falls_back_to_agent_name(overlay):
    assert da.bot_account_for_agent("nomad") == "nomad"


def test_token_key_comes_from_connector_config(overlay):
    assert da.token_key_for_account("engineer3") == "discord-engineer3"


def test_token_key_defaults_for_unconfigured_account(overlay):
    assert da.token_key_for_account("nomad") == "discord-nomad"


# --- the bug: no explicit bot must not mean "the main bot" ---

def test_default_uses_the_calling_agents_own_token(overlay):
    vault = StubVault({"discord-engineer3": "e3-tok", "discord-token": "main-tok"})
    token, err = da.resolve_bot_token(vault, agent_id="engineer3")
    assert (token, err) == ("e3-tok", None)


def test_default_does_not_reach_for_main_when_agent_has_an_account(overlay):
    """The precise 403 case: main's token would be accepted before this fix."""
    vault = StubVault({"discord-engineer3": "e3-tok", "discord-token": "main-tok"})
    token, _ = da.resolve_bot_token(vault, agent_id="engineer3")
    assert token != "main-tok"


def test_agent_without_own_token_still_gets_the_shared_default(overlay):
    """No own account configured — old shared-token behaviour, not an error."""
    vault = StubVault({"discord-token": "main-tok"})
    assert da.resolve_bot_token(vault, agent_id="nomad") == ("main-tok", None)


def test_active_token_wins_over_the_bare_default(overlay):
    vault = StubVault({"active-discord-token": "active-tok", "discord-token": "main-tok"})
    assert da.resolve_bot_token(vault, agent_id="nomad")[0] == "active-tok"


def test_no_agent_id_keeps_the_legacy_default(overlay):
    vault = StubVault({"discord-token": "main-tok"})
    assert da.resolve_bot_token(vault, agent_id="")[0] == "main-tok"


def test_no_token_anywhere_is_an_error(overlay):
    token, err = da.resolve_bot_token(StubVault({}), agent_id="nomad")
    assert token is None and "no Discord token available" in err


def test_no_vault_is_an_error(overlay):
    token, err = da.resolve_bot_token(None, agent_id="engineer3")
    assert token is None and "no vault access" in err


# --- explicit bot= never falls back to another identity ---

def test_explicit_bot_uses_the_configured_token_key(overlay):
    """In-process the vault holds discord-<account>; only the MCP subprocess
    aliases it to discord-token-<account>. Looking at the alias alone missed."""
    vault = StubVault({"discord-engineer3": "e3-tok", "discord-token": "main-tok"})
    assert da.resolve_bot_token(vault, bot="engineer3")[0] == "e3-tok"


def test_explicit_bot_accepts_the_mcp_alias(overlay):
    vault = StubVault({"discord-token-engineer3": "e3-tok"})
    assert da.resolve_bot_token(vault, bot="engineer3")[0] == "e3-tok"


def test_explicit_bot_missing_never_falls_back(overlay):
    vault = StubVault({"discord-token": "main-tok", "active-discord-token": "active-tok"})
    token, err = da.resolve_bot_token(vault, bot="rescue")
    assert token is None
    assert "rescue" in err


def test_explicit_bot_ignores_the_calling_agents_token(overlay):
    vault = StubVault({"discord-engineer3": "e3-tok"})
    token, err = da.resolve_bot_token(vault, bot="rescue", agent_id="engineer3")
    assert token is None and "rescue" in err


# --- wired through the tools ---

def test_headers_default_to_the_calling_agents_bot(overlay):
    vault = StubVault({"discord-engineer3": "e3-tok", "discord-token": "main-tok"})
    headers = _discord_headers(vault, agent_id="engineer3")
    assert headers["Authorization"] == "Bot e3-tok"


def test_headers_without_agent_id_are_unchanged(overlay):
    vault = StubVault({"discord-token": "main-tok"})
    assert _discord_headers(vault)["Authorization"] == "Bot main-tok"


async def test_send_discord_file_authenticates_as_the_calling_agent(overlay, tmp_path, monkeypatch):
    sent = {}

    def fake_resolve(vault, bot="", agent_id=""):
        sent["bot"], sent["agent_id"] = bot, agent_id
        return None, "Error: stop here"

    monkeypatch.setattr("src.tools.discord_tools.resolve_bot_token", fake_resolve)
    f = tmp_path / "a.txt"
    f.write_text("x")
    ctx = ToolContext(agent_id="engineer3", vault=StubVault({"discord-engineer3": "t"}))
    await send_discord_file(ctx, "1545777084629131404", str(f))
    assert sent == {"bot": "", "agent_id": "engineer3"}


async def test_send_discord_file_403_names_the_bot_that_was_used(overlay, tmp_path, monkeypatch):
    """The old message was bare HTTP 403, which invited an egress diagnosis."""
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setattr("src.tools.ingest.validate_file_path", lambda p: None)

    class FakeResp:
        status = 403

        async def text(self):
            return '{"message": "Missing Access", "code": 50001}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def post(self, *a, **k):
            return FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **k: FakeSession())
    monkeypatch.setattr("aiohttp.FormData", MagicMock)
    ctx = ToolContext(agent_id="engineer3", vault=StubVault({"discord-engineer3": "t"}))
    out = await send_discord_file(ctx, "1545777084629131404", str(f))
    assert "Missing Access" in out
    assert "engineer3" in out and "1545777084629131404" in out
