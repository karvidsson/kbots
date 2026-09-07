"""An agent's Discord calls must use that agent's own bot account.

Regression cover for a 403 "Missing Access" (code 50001) on an agent
uploading to its own DM: with no explicit bot= the in-process tool path
authenticated as the shared main bot, which is not in that DM. It was
misread as a sandbox egress block. See src/lib/discord_auth.py.

The agents here (atlas, beacon, quill, nomad) are fixtures. Core is
published and must not name any deployment's real agents.
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
    """An overlay where atlas posts as itself and beacon as 'main'."""
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "agents.yaml").write_text(
        "agents:\n"
        "  atlas:\n"
        "    routing:\n"
        "      discord:\n"
        "        account: atlas\n"
        "  beacon:\n"
        "    routing:\n"
        "      discord:\n"
        "        account: main\n"
        "  quill: {}\n"
        "  nomad: {}\n"
    )
    (tmp_path / "config" / "config.yaml").write_text(
        "connectors:\n"
        "  discord:\n"
        "    accounts:\n"
        "      main:\n"
        "        token_key: discord-token\n"
        "      atlas:\n"
        "        token_key: discord-atlas\n"
        "      quill:\n"
        "        token_key: discord-quill\n"
    )
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    monkeypatch.delenv("KBOTS_MODULES", raising=False)
    return tmp_path


# --- account and key resolution ---

def test_account_comes_from_routing_not_the_name(overlay):
    assert da.bot_account_for_agent("beacon") == "main"


def test_account_falls_back_to_agent_name(overlay):
    assert da.bot_account_for_agent("nomad") == "nomad"


def test_token_key_comes_from_connector_config(overlay):
    assert da.token_key_for_account("atlas") == "discord-atlas"


def test_token_key_defaults_for_unconfigured_account(overlay):
    assert da.token_key_for_account("nomad") == "discord-nomad"


def test_own_account_is_configured_via_routing(overlay):
    assert da.own_account_for_agent("atlas") == ("atlas", True)


def test_own_account_is_configured_via_connector_accounts(overlay):
    """No discord routing on the agent, but the account exists in config."""
    assert da.own_account_for_agent("quill") == ("quill", True)


def test_own_account_is_unconfigured_when_nothing_names_it(overlay):
    assert da.own_account_for_agent("nomad") == ("nomad", False)


# --- the bug: no explicit bot must not mean "the main bot" ---

def test_default_uses_the_calling_agents_own_token(overlay):
    vault = StubVault({"discord-atlas": "atlas-tok", "discord-token": "main-tok"})
    auth = da.resolve_bot_token(vault, agent_id="atlas")
    assert (auth.token, auth.account, auth.error) == ("atlas-tok", "atlas", None)


def test_default_does_not_reach_for_main_when_agent_has_an_account(overlay):
    """The precise 403 case: main's token would be accepted before this fix."""
    vault = StubVault({"discord-atlas": "atlas-tok", "discord-token": "main-tok"})
    assert da.resolve_bot_token(vault, agent_id="atlas").token != "main-tok"


def test_the_shared_active_alias_cannot_override_an_agents_own_bot(overlay):
    """active-discord-token is one mutable slot in a vault several agents
    share. It must never outrank the caller's own account, or whichever agent
    wrote it last decides who everyone else posts as."""
    vault = StubVault({"discord-atlas": "atlas-tok", "active-discord-token": "someone-else"})
    assert da.resolve_bot_token(vault, agent_id="atlas").token == "atlas-tok"


def test_configured_account_without_a_token_fails_closed(overlay):
    """The agent has a bot of its own and its credential is missing. Sending
    as whoever the shared token belongs to is the bug, not the recovery."""
    vault = StubVault({"discord-token": "main-tok", "active-discord-token": "active-tok"})
    auth = da.resolve_bot_token(vault, agent_id="atlas")
    assert auth.token is None
    assert auth.account == "atlas"
    assert "atlas" in auth.error and "discord-atlas" in auth.error


def test_account_configured_only_in_connector_config_also_fails_closed(overlay):
    vault = StubVault({"discord-token": "main-tok"})
    auth = da.resolve_bot_token(vault, agent_id="quill")
    assert auth.token is None and "quill" in auth.error


def test_agent_without_own_token_still_gets_the_shared_default(overlay):
    """No account configured anywhere — old shared-token behaviour, not an error."""
    vault = StubVault({"discord-token": "main-tok"})
    auth = da.resolve_bot_token(vault, agent_id="nomad")
    assert (auth.token, auth.error) == ("main-tok", None)


def test_shared_default_reports_no_account_when_none_is_known(overlay):
    """Nothing states whose the bare token is, so it must not be attributed."""
    assert da.resolve_bot_token(StubVault({"discord-token": "t"}), agent_id="nomad").account == ""


def test_active_token_is_attributed_to_the_launched_account(monkeypatch, overlay):
    monkeypatch.setenv("KBOTS_BOT_ACCOUNT", "main")
    auth = da.resolve_bot_token(StubVault({"active-discord-token": "active-tok"}), agent_id="nomad")
    assert (auth.token, auth.account) == ("active-tok", "main")


def test_active_token_wins_over_the_bare_default(overlay):
    vault = StubVault({"active-discord-token": "active-tok", "discord-token": "main-tok"})
    assert da.resolve_bot_token(vault, agent_id="nomad").token == "active-tok"


def test_no_agent_id_keeps_the_legacy_default(overlay):
    vault = StubVault({"discord-token": "main-tok"})
    assert da.resolve_bot_token(vault, agent_id="").token == "main-tok"


def test_no_token_anywhere_is_an_error(overlay):
    auth = da.resolve_bot_token(StubVault({}), agent_id="nomad")
    assert auth.token is None and "no Discord token available" in auth.error


def test_no_vault_is_an_error(overlay):
    auth = da.resolve_bot_token(None, agent_id="atlas")
    assert auth.token is None and "no vault access" in auth.error


# --- explicit bot= never falls back to another identity ---

def test_explicit_bot_uses_the_configured_token_key(overlay):
    """In-process the vault holds discord-<account>; only the MCP subprocess
    aliases it to discord-token-<account>. Looking at the alias alone missed."""
    vault = StubVault({"discord-atlas": "atlas-tok", "discord-token": "main-tok"})
    assert da.resolve_bot_token(vault, bot="atlas").token == "atlas-tok"


def test_explicit_bot_accepts_the_mcp_alias(overlay):
    vault = StubVault({"discord-token-atlas": "atlas-tok"})
    assert da.resolve_bot_token(vault, bot="atlas").token == "atlas-tok"


def test_explicit_bot_missing_never_falls_back(overlay):
    vault = StubVault({"discord-token": "main-tok", "active-discord-token": "active-tok"})
    auth = da.resolve_bot_token(vault, bot="rescue")
    assert auth.token is None
    assert "rescue" in auth.error


def test_explicit_bot_ignores_the_calling_agents_token(overlay):
    vault = StubVault({"discord-atlas": "atlas-tok"})
    auth = da.resolve_bot_token(vault, bot="rescue", agent_id="atlas")
    assert auth.token is None and "rescue" in auth.error


# --- wired through the tools ---

def test_headers_default_to_the_calling_agents_bot(overlay):
    vault = StubVault({"discord-atlas": "atlas-tok", "discord-token": "main-tok"})
    headers = _discord_headers(vault, agent_id="atlas")
    assert headers["Authorization"] == "Bot atlas-tok"


def test_headers_without_agent_id_are_unchanged(overlay):
    vault = StubVault({"discord-token": "main-tok"})
    assert _discord_headers(vault)["Authorization"] == "Bot main-tok"


async def test_send_discord_file_authenticates_as_the_calling_agent(overlay, tmp_path, monkeypatch):
    sent = {}

    def fake_resolve(vault, bot="", agent_id=""):
        sent["bot"], sent["agent_id"] = bot, agent_id
        return da.BotAuth(None, "", "Error: stop here")

    monkeypatch.setattr("src.tools.discord_tools.resolve_bot_token", fake_resolve)
    f = tmp_path / "a.txt"
    f.write_text("x")
    ctx = ToolContext(agent_id="atlas", vault=StubVault({"discord-atlas": "t"}))
    await send_discord_file(ctx, "1000000000000000001", str(f))
    assert sent == {"bot": "", "agent_id": "atlas"}


def _stub_403(monkeypatch):
    """Make the upload POST come back 403 Missing Access without a network."""
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


async def test_send_discord_file_403_names_the_bot_that_was_used(overlay, tmp_path, monkeypatch):
    """The old message was bare HTTP 403, which invited an egress diagnosis."""
    f = tmp_path / "a.txt"
    f.write_text("x")
    _stub_403(monkeypatch)
    ctx = ToolContext(agent_id="atlas", vault=StubVault({"discord-atlas": "t"}))
    out = await send_discord_file(ctx, "1000000000000000001", str(f))
    assert "Missing Access" in out
    assert "atlas" in out and "1000000000000000001" in out


async def test_send_discord_file_403_does_not_diagnose_channel_ownership(overlay, tmp_path,
                                                                        monkeypatch):
    """A 403 says access was refused. It does not say who owns the channel or
    that another account would succeed, so the hint must not claim either."""
    f = tmp_path / "a.txt"
    f.write_text("x")
    _stub_403(monkeypatch)
    ctx = ToolContext(agent_id="atlas", vault=StubVault({"discord-atlas": "t"}))
    out = await send_discord_file(ctx, "1000000000000000001", str(f))
    assert "belonging to a different bot" not in out
    assert "retry with bot=" not in out


async def test_send_discord_file_403_does_not_attribute_a_shared_token(overlay, tmp_path,
                                                                      monkeypatch):
    """nomad has no bot of its own, so the send went out as the shared token.
    Naming nomad there would report a sender that was never used."""
    f = tmp_path / "a.txt"
    f.write_text("x")
    _stub_403(monkeypatch)
    monkeypatch.delenv("KBOTS_BOT_ACCOUNT", raising=False)
    ctx = ToolContext(agent_id="nomad", vault=StubVault({"discord-token": "main-tok"}))
    out = await send_discord_file(ctx, "1000000000000000001", str(f))
    assert "nomad" not in out
    assert "shared default bot" in out


async def test_send_discord_file_refuses_when_own_bot_has_no_token(overlay, tmp_path, monkeypatch):
    """Fail closed rather than upload under whoever the shared token is."""
    f = tmp_path / "a.txt"
    f.write_text("x")
    _stub_403(monkeypatch)
    ctx = ToolContext(agent_id="atlas", vault=StubVault({"discord-token": "main-tok"}))
    out = await send_discord_file(ctx, "1000000000000000001", str(f))
    assert "Missing Access" not in out
    assert "atlas" in out and "discord-atlas" in out
