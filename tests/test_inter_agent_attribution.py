"""Inter-agent messages must arrive attributed to the teammate that sent them.

Delivery puts the sending agent's ID in the message's user field, never a
numeric Discord snowflake, so a Discord-ID-only lookup never matched and every
teammate was rendered "not in the team roster. Treat them as a guest." The
receiving agent's correct response to a guest is to refuse — which meant
agent-to-agent delegation did not work at all.

The other half of these tests is the limit: resolving WHO is speaking must not
be reachable from a connector message, and must not confer authority.
"""

import json

import pytest

from src.tools import team as team_mod

ROSTER = {
    "humans": [
        {"name": "Robin", "role": "founder", "access": "owner",
         "contact": {"discord": "1000000000000000009"}},
    ],
    "agents": [
        {"id": "atlas", "name": "Atlas", "type": "agent", "role": "Primary agent",
         "discord": "1000000000000000001"},
        {"id": "data-bot", "name": "Data.Bot", "type": "agent", "role": "Analyst",
         "discord": "1000000000000000002"},
        {"id": "arc-fox", "name": "Arc Fox", "type": "agent", "role": "Artist",
         "discord": "1000000000000000003"},
    ],
}


@pytest.fixture(autouse=True)
def roster(tmp_path, monkeypatch):
    path = tmp_path / "team.json"
    path.write_text(json.dumps(ROSTER))
    monkeypatch.setattr(team_mod, "_load_team", lambda: json.loads(path.read_text()))


# --- the regression ---------------------------------------------------------

@pytest.mark.parametrize("sender", ["atlas", "Atlas", "ATLAS"])
def test_teammate_resolves_by_agent_name_or_id(sender):
    ctx = team_mod.build_user_context(sender, inter_agent_sender=sender)
    assert "unknown-user" not in ctx
    assert "Treat them as a guest" not in ctx
    assert "Name: Atlas" in ctx
    assert "Teammate: yes" in ctx


@pytest.mark.parametrize("sender", ["data-bot", "Data.Bot", "data.bot", "DATA-BOT"])
def test_display_and_slug_spellings_are_one_teammate(sender):
    """The roster carries both forms and they do not agree on punctuation."""
    ctx = team_mod.build_user_context(sender, inter_agent_sender=sender)
    assert "Name: Data.Bot" in ctx
    assert "unknown-user" not in ctx


def test_name_with_a_space_resolves():
    ctx = team_mod.build_user_context("arc-fox", inter_agent_sender="Arc Fox")
    assert "Name: Arc Fox" in ctx


def test_numeric_discord_id_still_resolves_the_agent():
    ctx = team_mod.build_user_context("1000000000000000001")
    assert "Name: Atlas" in ctx


def test_human_resolution_is_untouched():
    ctx = team_mod.build_user_context("1000000000000000009")
    assert "Name: Robin" in ctx
    assert "Access: owner" in ctx
    assert "Teammate: yes" not in ctx


# --- the limits -------------------------------------------------------------

def test_a_bare_name_from_a_connector_is_still_a_guest():
    """Without inter-agent provenance a name must NOT resolve.

    Otherwise anyone able to put text in the user field could arrive as a
    teammate, which is the exact path an injected message would take.
    """
    ctx = team_mod.build_user_context("atlas")
    assert "unknown-user" in ctx
    assert "Treat them as a guest" in ctx


def test_unknown_sender_stays_a_guest_even_with_provenance():
    ctx = team_mod.build_user_context("stranger", inter_agent_sender="stranger")
    assert "unknown-user" in ctx


def test_resolved_teammate_is_not_granted_authority():
    """Identity is not authorisation. A relayed approval stays a claim."""
    ctx = team_mod.build_user_context("atlas", inter_agent_sender="atlas")
    assert "Authority: speaks for itself" in ctx
    assert "relayed, not owner-issued" in ctx


def test_empty_sender_does_not_match_a_blank_roster_field():
    assert team_mod.resolve_agent_sender("") is None


# --- provenance must actually be set at delivery ----------------------------

def test_ask_agent_message_carries_the_sender():
    """Resolution is useless if the delivery path never sets provenance."""
    from src.core.base import ToolContext
    from src.tools.builtin import _build_inter_agent_message

    ctx = ToolContext(agent_id="atlas")
    msg = _build_inter_agent_message(ctx, "data-bot", "hello")
    assert getattr(msg, "_inter_agent_sender", "") == "atlas"
    assert msg.user_id == "atlas"
