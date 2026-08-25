"""The guild-join hook: who runs it, when it runs, and what it never does twice.

Provisioning is idempotent, so the hook's job is narrower than it looks: pick
exactly one bot, refuse to run again for a guild already done, and hand the
agent a turn only after the channels are actually wired.
"""

import types

import pytest

from src.connectors.discord import DiscordBot
from src.core import runtime_state, server_setup


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


def _agent(account, tier=""):
    cfg = {"routing": {"discord": {"account": account}}, "display_name": account.title()}
    if tier:
        cfg["tier"] = tier
    return cfg


def _bot(account, data_dir, configs=None, full_config=None, on_setup=None,
         profile=""):
    connector = types.SimpleNamespace(
        config={},
        _agent_configs=configs if configs is not None else {"j": _agent("main")},
        _full_config=full_config if full_config is not None else {},
        _data_dir=str(data_dir),
        _on_guild_setup=on_setup,
        _setup_profile=profile,
    )
    bot = DiscordBot(account_name=account, connector=connector, admin_users=[])
    bot._token = "tok"
    return bot


@pytest.fixture
def provisioned(monkeypatch):
    """Record every provisioning call so a test can assert it did NOT happen."""
    calls = []

    async def _provision(guild_id, token, config, specs=server_setup.SPECS):
        calls.append(guild_id)
        return [server_setup.Outcome("approvals", "kbots-approvals", "111", "created"),
                server_setup.Outcome("platform_updates", "platform-updates",
                                     "222", "created")]

    monkeypatch.setattr(server_setup, "provision_guild", _provision)
    return calls


_GUILD = types.SimpleNamespace(id=42, name="ME")


async def test_the_setup_bot_provisions_wires_and_marks_the_guild(overlay, provisioned):
    turns = []

    async def _on_setup(agent_id, guild_id, guild_name, outcomes):
        turns.append((agent_id, guild_id, [o.key for o in outcomes]))

    await _bot("main", overlay, on_setup=_on_setup).on_guild_join(_GUILD)

    assert provisioned == ["42"]
    assert runtime_state.get_flag("hitl_channel") == "111"
    assert server_setup.guild_is_set_up(overlay, "42")
    assert turns == [("j", "42", ["approvals", "platform_updates"])]


async def test_a_non_setup_bot_does_nothing(overlay, provisioned):
    configs = {"j": _agent("main"), "e": _agent("engineer")}
    await _bot("engineer", overlay, configs=configs).on_guild_join(_GUILD)

    assert provisioned == []
    assert not server_setup.guild_is_set_up(overlay, "42")


async def test_a_reinvite_does_not_reprovision(overlay, provisioned):
    server_setup.record_guild_setup(overlay, "42", {"name": "ME"})
    await _bot("main", overlay).on_guild_join(_GUILD)

    assert provisioned == []


async def test_the_hook_can_be_turned_off(overlay, provisioned):
    await _bot("main", overlay,
               full_config={"server_setup": {"on_guild_join": False}}).on_guild_join(_GUILD)

    assert provisioned == []


async def test_the_hook_is_on_by_default(overlay, provisioned):
    """Zero-setup is the whole point, so an install with no server_setup block
    at all must still provision."""
    await _bot("main", overlay, full_config={}).on_guild_join(_GUILD)

    assert provisioned == ["42"]


async def test_a_failing_agent_turn_leaves_the_server_wired(overlay, provisioned):
    """The channels are the deliverable. An LLM turn that throws must not undo
    them or stop the guild being marked done."""
    async def _boom(*a):
        raise RuntimeError("model unavailable")

    await _bot("main", overlay, on_setup=_boom).on_guild_join(_GUILD)

    assert runtime_state.get_flag("hitl_channel") == "111"
    assert server_setup.guild_is_set_up(overlay, "42")


async def test_provisioning_that_raises_marks_nothing(overlay, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("discord down")

    monkeypatch.setattr(server_setup, "provision_guild", _boom)
    await _bot("main", overlay).on_guild_join(_GUILD)

    assert not server_setup.guild_is_set_up(overlay, "42"), \
        "a guild marked done after a failed run would never be retried"


async def test_a_secondary_instance_never_provisions(overlay, provisioned):
    """The rescue instance loads only agents.rescue.yaml, so its single agent
    elects itself setup account — which is how a dual-instance install created
    every channel twice. The profile gate closes that hole."""
    configs = {"e": _agent("engineer")}          # len==1 → would self-elect
    await _bot("engineer", overlay, configs=configs,
               profile="rescue").on_guild_join(_GUILD)

    assert provisioned == []
    assert not server_setup.guild_is_set_up(overlay, "42")


async def test_a_claimed_guild_is_not_provisioned_again(overlay, provisioned):
    """The done-marker lands after provisioning; the claim is what stops two
    near-simultaneous joins from racing to create duplicate channels."""
    assert server_setup.claim_guild_setup(overlay, "42", "other")
    await _bot("main", overlay).on_guild_join(_GUILD)

    assert provisioned == []


def test_claim_has_exactly_one_winner(tmp_path):
    assert server_setup.claim_guild_setup(tmp_path, "7", "a") is True
    assert server_setup.claim_guild_setup(tmp_path, "7", "b") is False
    server_setup.release_guild_claim(tmp_path, "7")
    assert server_setup.claim_guild_setup(tmp_path, "7", "b") is True


async def test_a_failed_run_releases_the_claim(overlay, monkeypatch):
    """Adopt-before-create makes a retry safe, so a failure must not leave a
    dead claim that blocks the retry forever."""
    async def _boom(*a, **k):
        raise RuntimeError("discord down")

    monkeypatch.setattr(server_setup, "provision_guild", _boom)
    await _bot("main", overlay).on_guild_join(_GUILD)

    assert server_setup.claim_guild_setup(overlay, "42", "retry") is True
