"""An agent owning its own Discord account name, and the roster row behind it."""

import json
from pathlib import Path

import pytest

from src.core import identity_boot as ib

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_runtime_flags():
    """Opt out of conftest's runtime-flag isolation: the flag-file location is
    part of what these tests assert. See tests/test_overlay_state_paths.py."""
    return None

CONFIGS = {
    "botson": {"display_name": "Botson",
               "routing": {"discord": {"account": "botson"}}},
    "atlas": {"display_name": "Atlas",
              "routing": {"discord": {"account": "atlas"}}},
}


# --- account to agent -------------------------------------------------------

def test_an_account_resolves_to_the_agent_that_routes_to_it():
    assert ib.agent_for_account(CONFIGS, "discord", "botson") == "botson"
    assert ib.agent_for_account(CONFIGS, "discord", "atlas") == "atlas"


def test_an_unclaimed_account_resolves_to_nothing():
    assert ib.agent_for_account(CONFIGS, "discord", "stranger") is None


def test_a_single_agent_install_may_omit_the_account_key():
    solo = {"solo": {"display_name": "Solo", "routing": {"discord": {}}}}
    assert ib.agent_for_account(solo, "discord", "default") == "solo"


def test_the_connector_name_is_honoured():
    assert ib.agent_for_account(CONFIGS, "slack", "botson") is None


def test_configured_name_falls_back_to_the_agent_id():
    assert ib.configured_name({"x": {}}, "x") == "x"
    assert ib.configured_name(CONFIGS, "botson") == "Botson"


# --- what counts as a mismatch ---------------------------------------------

def test_a_real_mismatch_is_a_mismatch():
    assert ib.names_differ("PM-One", "Botson") is True


def test_case_and_whitespace_alone_are_not_worth_a_rename():
    """Discord allows two username changes an hour.

    Spending one on a capital letter would mean a turn on every boot for a
    difference nobody can see.
    """
    assert ib.names_differ("botson", "Botson") is False
    assert ib.names_differ(" Botson ", "Botson") is False


def test_a_missing_name_is_never_a_mismatch():
    assert ib.names_differ("", "Botson") is False
    assert ib.names_differ("Botson", "") is False


# --- owner lookup -----------------------------------------------------------

def test_the_owner_is_preferred_over_other_humans():
    team = {"humans": [
        {"name": "Someone", "access": "member", "contact": {"discord": "111"}},
        {"name": "Boss", "access": "owner", "contact": {"discord": "222"}},
    ]}
    assert ib.owner_discord_id(team) == "222"


def test_the_first_human_with_a_discord_id_is_the_fallback():
    team = {"humans": [
        {"name": "No Contact", "access": "member"},
        {"name": "Someone", "access": "member", "contact": {"discord": "333"}},
    ]}
    assert ib.owner_discord_id(team) == "333"


def test_an_empty_roster_yields_no_owner():
    assert ib.owner_discord_id({}) == ""


# --- what needs fixing ------------------------------------------------------

def test_only_the_mismatched_account_is_pending(tmp_path):
    live = {"botson": "PM-One", "atlas": "Atlas"}
    pending = ib.pending_renames(live, CONFIGS, tmp_path)
    assert [p["agent_id"] for p in pending] == ["botson"]
    assert pending[0] == {"agent_id": "botson", "account": "botson",
                          "live_name": "PM-One", "configured_name": "Botson"}


def test_a_matching_fleet_produces_no_turns_at_all(tmp_path):
    live = {"botson": "Botson", "atlas": "Atlas"}
    assert ib.pending_renames(live, CONFIGS, tmp_path) == []


def test_a_rename_discord_keeps_refusing_stops_after_the_cap(tmp_path):
    """Without a cap this is one LLM turn per restart, forever.

    The trigger is self-limiting only when the rename SUCCEEDS. A name Discord
    will not accept never clears the mismatch, so the cap is what bounds it.
    """
    live = {"botson": "PM-One"}
    for _ in range(ib.MAX_ATTEMPTS):
        assert len(ib.pending_renames(live, CONFIGS, tmp_path)) == 1
        ib.record_attempt(tmp_path, "botson", "Botson")
    assert ib.pending_renames(live, CONFIGS, tmp_path) == []


def test_the_cap_is_per_target_name_so_changing_the_config_retries(tmp_path):
    live = {"botson": "PM-One"}
    for _ in range(ib.MAX_ATTEMPTS):
        ib.record_attempt(tmp_path, "botson", "Botson")
    assert ib.pending_renames(live, CONFIGS, tmp_path) == []
    renamed = {"botson": {"display_name": "Bot Son",
                          "routing": {"discord": {"account": "botson"}}}}
    assert len(ib.pending_renames(live, renamed, tmp_path)) == 1


def test_an_unwritable_attempt_store_does_not_break_the_check(tmp_path):
    missing = tmp_path / "no" / "such" / "dir"
    assert ib.attempts_for(missing, "botson", "Botson") == 0
    ib.record_attempt(missing / "deeper", "botson", "Botson")  # must not raise


# --- the synthetic turn -----------------------------------------------------

def test_the_prompt_names_the_tools_and_asks_for_silence():
    msg = ib.build_identity_message(
        {"agent_id": "botson", "account": "botson",
         "live_name": "PM-One", "configured_name": "Botson"},
        "999", "discord", "c1")
    assert msg.channel_id == "c1"
    assert msg.bot_account == "botson"
    assert msg.user_name == "identity-reconcile"
    assert "discord_set_bot_name(name='Botson', bot='botson')" in msg.content
    assert "set_agent_avatar(agent_name='botson')" in msg.content
    assert "user_id='999'" in msg.content
    # The turn has no human waiting on it; a reply would post into the agent's
    # own home channel.
    assert "NO_REPLY" in msg.content
    # Two changes per hour: a refused rename must not be retried inside the turn.
    assert "do NOT retry" in msg.content


def test_no_owner_on_the_roster_means_no_guessed_recipient():
    msg = ib.build_identity_message(
        {"agent_id": "botson", "account": "botson",
         "live_name": "PM-One", "configured_name": "Botson"},
        "", "discord", "c1")
    assert "skip the greeting rather than guessing" in msg.content


# --- the roster row this was really about -----------------------------------

def test_the_roster_records_the_configured_name_not_the_discord_one(monkeypatch, tmp_path):
    """`on_ready` passed client.user.name.

    An account named differently from its agent added a roster row under a name
    no config knows. That row is the name the agent then reads back as its own
    and hands to set_agent_avatar, which resolves it to a vault key that was
    never created and fails with "no bot token found".
    """
    from src.tools import team as team_mod
    roster = tmp_path / "team.json"
    roster.write_text(json.dumps({"humans": [], "agents": [
        {"id": "botson", "name": "Botson", "type": "agent"}]}))
    monkeypatch.setattr(team_mod, "TEAM_FILE", roster)

    agent_id = ib.agent_for_account(CONFIGS, "discord", "botson")
    team_mod.record_bot_identity(ib.configured_name(CONFIGS, agent_id), "12345")

    saved = json.loads(roster.read_text())
    assert [a["id"] for a in saved["agents"]] == ["botson"]
    assert saved["agents"][0]["discord"] == "12345"


def test_the_live_name_would_have_added_a_stub_row(monkeypatch, tmp_path):
    from src.tools import team as team_mod
    roster = tmp_path / "team.json"
    roster.write_text(json.dumps({"humans": [], "agents": [
        {"id": "botson", "name": "Botson", "type": "agent"}]}))
    monkeypatch.setattr(team_mod, "TEAM_FILE", roster)

    team_mod.record_bot_identity("PM-One", "12345")  # the old behaviour

    saved = json.loads(roster.read_text())
    assert len(saved["agents"]) == 2, "the stub row this fix exists to prevent"


# --- the two wirings, pinned where they actually live -----------------------

def test_on_ready_no_longer_passes_the_live_discord_name():
    """The roster row is the whole reason the avatar tool could not find a token.

    Checked at the source because the alternative is standing up a Discord
    client, and what matters is that this one call never regresses.
    """
    src = (REPO / "src/connectors/discord.py").read_text()
    block = src.split("async def on_ready")[1].split("\n    async def ")[0]
    assert "record_bot_identity(self.client.user.name" not in block
    assert "agent_for_account(" in block


def test_the_boot_reconcile_is_off_unless_a_config_asks_for_it():
    """Renaming a bot account is outward-facing and capped at two per hour.

    An established fleet must not discover this after a restart, so the default
    has to be off, not merely documented as off.
    """
    src = (REPO / "src/main.py").read_text()
    assert '.get("reconcile_on_boot", False)' in src
    example = (REPO / "config/config.yaml.example").read_text()
    assert "reconcile_on_boot: false" in example


# --- runtime_state: the same read-only-store bug, in a third module ---------

@pytest.fixture
def overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


def test_flags_are_written_under_data_not_the_overlay_root(overlay):
    from src.core import runtime_state as rs
    rs.set_flag("hitl_enabled", False)
    assert (overlay / "data" / "runtime.json").exists()
    assert not (overlay / "runtime.json").exists()
    assert rs.get_flag("hitl_enabled") is False


def test_a_legacy_root_file_still_governs_until_the_first_write(overlay):
    from src.core import runtime_state as rs
    (overlay / "runtime.json").write_text(json.dumps({"hitl_enabled": False}))
    assert rs.get_flag("hitl_enabled") is False


def test_legacy_flags_are_carried_forward_on_the_first_write(overlay):
    """A killswitch someone left ON must survive the move."""
    from src.core import runtime_state as rs
    (overlay / "runtime.json").write_text(json.dumps({"hitl_enabled": False}))
    rs.set_flag("schedule_board", "c9")
    migrated = json.loads((overlay / "data" / "runtime.json").read_text())
    assert migrated == {"hitl_enabled": False, "schedule_board": "c9"}


def test_clearing_a_legacy_only_flag_writes_the_current_file(overlay):
    """Editing the legacy file in place would let the flag come back.

    The next set_flag migrates the legacy copy forward, so a flag cleared only
    there would reappear.
    """
    from src.core import runtime_state as rs
    (overlay / "runtime.json").write_text(json.dumps({"hitl_enabled": False}))
    rs.clear_flag("hitl_enabled")
    assert rs.get_flag("hitl_enabled", "unset") == "unset"
    rs.set_flag("other", 1)
    assert rs.get_flag("hitl_enabled", "unset") == "unset"


def test_the_current_file_wins_over_a_stale_legacy_one(overlay):
    from src.core import runtime_state as rs
    (overlay / "runtime.json").write_text(json.dumps({"hitl_enabled": False}))
    (overlay / "data").mkdir()
    (overlay / "data" / "runtime.json").write_text(json.dumps({"hitl_enabled": True}))
    assert rs.get_flag("hitl_enabled") is True
