"""Server auto-setup: provision fleet channels on guild join, safely.

The three properties that make this safe to fire at a live server: adopt before
create, never repoint a role that is already set, and converge on re-run.
"""

import json

import pytest

from src.core import runtime_state, server_setup
from src.core.server_setup import SPECS, ChannelSpec, Outcome


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


@pytest.fixture
def guild(monkeypatch):
    """A fake Discord server. Records every channel POST so a test can assert
    that nothing was created, which is the interesting half of the contract."""
    state = {"channels": [], "created": [], "fail_status": 0}

    async def _list(guild_id, token):
        return list(state["channels"])

    async def _create(guild_id, token, spec):
        if state["fail_status"] == 403:
            return "", ("missing the Manage Channels permission "
                        "(re-invite the bot with it, then re-run setup)")
        new_id = str(9000 + len(state["created"]))
        state["created"].append(spec.name)
        state["channels"].append({"id": new_id, "name": spec.name, "type": spec.kind})
        return new_id, ""

    monkeypatch.setattr(server_setup, "_list_channels", _list)
    monkeypatch.setattr(server_setup, "_create_channel", _create)
    return state


# --- create ----------------------------------------------------------------

async def test_empty_server_gets_every_role_created_and_wired(overlay, guild):
    outcomes = await server_setup.provision_guild("1", "tok", {})
    server_setup.wire(outcomes)

    assert {o.action for o in outcomes} == {"created"}
    assert len(outcomes) == len(SPECS)
    for spec in SPECS:
        assert runtime_state.get_flag(spec.flag), f"{spec.key} was not wired"


async def test_the_goals_role_is_created_as_a_category_not_a_channel(overlay, guild):
    await server_setup.provision_guild("1", "tok", {})
    goals = next(c for c in guild["channels"] if c["name"] == "kbots-goals")
    assert goals["type"] == server_setup.TYPE_CATEGORY


# --- adopt before create ---------------------------------------------------

async def test_an_existing_channel_is_adopted_not_duplicated(overlay, guild):
    guild["channels"] = [{"id": "555", "name": "approvals", "type": 0}]

    outcomes = await server_setup.provision_guild("1", "tok", {})
    server_setup.wire(outcomes)

    approvals = next(o for o in outcomes if o.key == "approvals")
    assert approvals.action == "adopted" and approvals.channel_id == "555"
    assert "kbots-approvals" not in guild["created"]
    assert runtime_state.get_flag("hitl_channel") == "555"


async def test_a_name_match_of_the_wrong_type_is_not_adopted(overlay, guild):
    """A text channel called 'goals' is not the goals CATEGORY. Adopting it
    would put every goal channel's parent_id on something that cannot parent."""
    guild["channels"] = [{"id": "555", "name": "goals", "type": 0}]

    outcomes = await server_setup.provision_guild("1", "tok", {})

    goals = next(o for o in outcomes if o.key == "goals")
    assert goals.action == "created" and goals.channel_id != "555"


# --- never repoint ---------------------------------------------------------

async def test_a_role_set_in_config_is_left_alone(overlay, guild):
    config = {"security": {"alert_channel": "777"}}

    outcomes = await server_setup.provision_guild("1", "tok", config)
    server_setup.wire(outcomes)

    alerts = next(o for o in outcomes if o.key == "alerts")
    assert alerts.action == "kept" and alerts.channel_id == "777"
    assert "kbots-alerts" not in guild["created"]
    # And critically: wire() must not promote a config value into a runtime
    # flag, or config would stop governing that role forever after.
    assert runtime_state.get_flag("alert_channel") is None


async def test_a_role_set_by_a_runtime_override_is_left_alone(overlay, guild):
    runtime_state.set_flag("hitl_channel", "888")

    outcomes = await server_setup.provision_guild("1", "tok", {})

    approvals = next(o for o in outcomes if o.key == "approvals")
    assert approvals.action == "kept" and approvals.channel_id == "888"
    assert "kbots-approvals" not in guild["created"]


# --- converge --------------------------------------------------------------

async def test_running_twice_creates_nothing_the_second_time(overlay, guild):
    server_setup.wire(await server_setup.provision_guild("1", "tok", {}))
    created_first = list(guild["created"])

    second = await server_setup.provision_guild("1", "tok", {})

    assert guild["created"] == created_first
    assert {o.action for o in second} == {"kept"}


# --- failure ---------------------------------------------------------------

async def test_missing_manage_channels_fails_loudly_and_wires_nothing(overlay, guild):
    guild["fail_status"] = 403

    outcomes = await server_setup.provision_guild("1", "tok", {})
    server_setup.wire(outcomes)

    assert {o.action for o in outcomes} == {"failed"}
    assert all("Manage Channels" in o.detail for o in outcomes)
    for spec in SPECS:
        assert runtime_state.get_flag(spec.flag) is None


async def test_unreadable_server_fails_every_role_rather_than_creating_blind(
        overlay, guild, monkeypatch):
    """Without the channel list there is no way to know what already exists, and
    creating anyway is how a server ends up with two of everything."""
    async def _no_list(guild_id, token):
        return None
    monkeypatch.setattr(server_setup, "_list_channels", _no_list)

    outcomes = await server_setup.provision_guild("1", "tok", {})

    assert {o.action for o in outcomes} == {"failed"}
    assert guild["created"] == []


# --- which bot does it -----------------------------------------------------

def _agent(account, tier=""):
    cfg = {"routing": {"discord": {"account": account}}}
    if tier:
        cfg["tier"] = tier
    return cfg


def test_only_one_bot_is_the_setup_account():
    configs = {"a": _agent("main"), "b": _agent("ledger"), "c": _agent("engineer")}
    picked = [acct for acct in ("main", "ledger", "engineer")
              if server_setup.is_setup_account(configs, acct)]
    assert picked == ["main"]


def test_a_coordinator_outranks_the_main_account():
    configs = {"a": _agent("main"), "b": _agent("boss", tier="coordinator")}
    assert server_setup.is_setup_account(configs, "boss")
    assert not server_setup.is_setup_account(configs, "main")


def test_a_single_agent_install_is_the_setup_account_whatever_it_is_called():
    assert server_setup.is_setup_account({"solo": _agent("solo")}, "solo")


# --- the guild marker ------------------------------------------------------

def test_guild_marker_stops_a_reinvite_from_reprovisioning(tmp_path):
    assert server_setup.guild_is_set_up(tmp_path, "42") is False
    server_setup.record_guild_setup(tmp_path, "42", {"name": "ME"})
    assert server_setup.guild_is_set_up(tmp_path, "42") is True
    assert server_setup.guild_is_set_up(tmp_path, "43") is False

    # A second guild must not overwrite the first.
    server_setup.record_guild_setup(tmp_path, "43", {"name": "OTHER"})
    data = json.loads((tmp_path / server_setup.MARKER_FILE).read_text())
    assert set(data["guilds"]) == {"42", "43"}


def test_an_unwritable_marker_does_not_raise(tmp_path):
    """Provisioning is idempotent, so a lost marker is noise, not damage. It
    must not take the join handler down with it."""
    (tmp_path / "blocked").write_text("not a directory")
    server_setup.record_guild_setup(tmp_path / "blocked" / "deeper", "42", {})


# --- the agent's turn ------------------------------------------------------

def test_the_setup_turn_names_the_channels_and_the_agent_identity():
    outcomes = [Outcome("approvals", "kbots-approvals", "111", "created"),
                Outcome("alerts", "kbots-alerts", "222", "adopted")]
    msg = server_setup.build_setup_message(
        "jarvis", "1", "ME", outcomes, "discord", "222",
        display_name="Jarvis", account="main")

    assert msg.channel_id == "222" and msg.bot_account == "main"
    assert "<#111>" in msg.content and "<#222>" in msg.content
    assert "set_agent_avatar(agent_name='jarvis')" in msg.content
    # The guild_id is not decoration: discord_set_nickname with an empty
    # guild_id renames the bot in EVERY server it is in, so joining a second
    # server would silently rename it in the first.
    assert ("discord_set_nickname(nickname='Jarvis', bot='main', guild_id='1')"
            in msg.content)


def test_the_setup_turn_tells_the_agent_to_report_what_failed():
    outcomes = [Outcome("approvals", "kbots-approvals", "111", "created"),
                Outcome("goals", "kbots-goals", "", "failed",
                        "missing the Manage Channels permission")]
    msg = server_setup.build_setup_message("jarvis", "1", "ME", outcomes,
                                           "discord", "111")
    assert "could NOT be set up" in msg.content
    assert "Manage Channels" in msg.content


def test_a_clean_setup_turn_has_no_failure_note():
    msg = server_setup.build_setup_message(
        "jarvis", "1", "ME", [Outcome("approvals", "n", "111", "created")],
        "discord", "111")
    assert "could NOT" not in msg.content


# --- the spec itself -------------------------------------------------------

def test_every_spec_adopts_its_own_created_name():
    """Otherwise setup creates #kbots-alerts, then on the next run does not
    recognise it, and creates it again."""
    for spec in SPECS:
        assert spec.name in spec.aliases, f"{spec.key} would not adopt itself"


def test_flags_and_config_paths_are_unique():
    assert len({s.flag for s in SPECS}) == len(SPECS)
    assert len({s.config_path for s in SPECS}) == len(SPECS)


def test_config_lookup_walks_nested_keys():
    spec = ChannelSpec("x", "n", ("n",), 0, "", "f", ("a", "b", "c"))
    assert server_setup._config_value({"a": {"b": {"c": "5"}}}, spec.config_path) == "5"
    assert server_setup._config_value({"a": {"b": {}}}, spec.config_path) == ""
    assert server_setup._config_value({}, spec.config_path) == ""
