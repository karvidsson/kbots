"""Join introductions — every bot's agent introduces itself in the updates
channel when its account joins a server, exactly once per (guild, agent).
The setup agent's provisioning turn already includes its introduction."""

import asyncio
import types

import pytest

from src.connectors.discord import DiscordBot
from src.core import server_setup


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


def _agent(account):
    return {"routing": {"discord": {"account": account}},
            "display_name": account.title()}


CFG_WITH_CHANNEL = {"platform": {"updates_channel": "555"}}


def _bot(account, data_dir, configs, on_intro=None, profile="",
         full_config=None):
    connector = types.SimpleNamespace(
        config={},
        _agent_configs=configs,
        _full_config=CFG_WITH_CHANNEL if full_config is None else full_config,
        _data_dir=str(data_dir),
        _on_guild_setup=None,
        _on_guild_intro=on_intro,
        _setup_profile=profile,
    )
    bot = DiscordBot(account_name=account, connector=connector, admin_users=[])
    bot._token = "tok"
    return bot


@pytest.fixture
def intros():
    calls = []

    async def _on_intro(agent_id, guild_id, guild_name, channel_id):
        calls.append((agent_id, guild_id, channel_id))
    _on_intro.calls = calls
    return _on_intro


# --- channel resolution / marker plumbing ---

def test_intro_channel_prefers_updates_then_alerts(overlay):
    assert server_setup.intro_channel(CFG_WITH_CHANNEL) == "555"
    assert server_setup.intro_channel(
        {"security": {"alert_channel": "666"}}) == "666"
    assert server_setup.intro_channel({}) == ""


def test_intro_marker_round_trip(tmp_path):
    assert not server_setup.intro_done(tmp_path, "42", "e")
    server_setup.record_intro(tmp_path, "42", "e")
    assert server_setup.intro_done(tmp_path, "42", "e")
    assert not server_setup.intro_done(tmp_path, "42", "j")


# --- who introduces, and when ---

async def test_non_setup_bot_introduces_itself_once(overlay, intros):
    configs = {"j": _agent("main"), "e": _agent("engineer")}
    bot = _bot("engineer", overlay, configs, on_intro=intros)

    await bot._join_intro("42", "P2")
    await bot._join_intro("42", "P2")   # a re-add must not repeat it

    assert intros.calls == [("e", "42", "555")]


async def test_rescue_profile_bot_introduces_itself(overlay, intros):
    """The secondary instance never provisions — but its agent still owes the
    server an introduction."""
    bot = _bot("engineer", overlay, {"e": _agent("engineer")},
               on_intro=intros, profile="rescue")
    await bot._join_intro("42", "P2")

    assert intros.calls == [("e", "42", "555")]


async def test_setup_bot_leaves_intro_to_its_provisioning_turn(overlay, intros):
    configs = {"j": _agent("main")}
    bot = _bot("main", overlay, configs, on_intro=intros)
    await bot._join_intro("42", "P2")

    assert intros.calls == []


async def test_no_channel_ever_wired_skips_quietly(overlay, intros, monkeypatch):
    async def _no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    configs = {"j": _agent("main"), "e": _agent("engineer")}
    bot = _bot("engineer", overlay, configs, on_intro=intros, full_config={})
    await bot._join_intro("42", "P2")

    assert intros.calls == []
    # and nothing was marked done — a later join may still succeed
    assert not server_setup.intro_done(overlay, "42", "e")


async def test_intro_marked_even_when_the_turn_fails(overlay):
    """One introduction per (guild, agent) — a flubbed model turn must not
    turn every re-add into another attempt."""
    async def _boom(*a):
        raise RuntimeError("model unavailable")

    configs = {"j": _agent("main"), "e": _agent("engineer")}
    bot = _bot("engineer", overlay, configs, on_intro=_boom)
    await bot._join_intro("42", "P2")

    assert server_setup.intro_done(overlay, "42", "e")
