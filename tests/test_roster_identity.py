"""Roster identity — bots record their Discord id so teammates aren't seen as guests."""

import json

from src.core import startup_context
from src.tools import team


def test_record_bot_identity_updates_and_resolves(tmp_path, monkeypatch):
    tf = tmp_path / "team.json"
    tf.write_text(json.dumps({"humans": [], "agents": [
        {"id": "beacon", "name": "Beacon", "type": "agent",
         "role": "Monetization", "agent_tier": "coordinator"},
    ]}))
    monkeypatch.setattr(team, "TEAM_FILE", tf)

    # sets discord WITHOUT clobbering role / tier
    team.record_bot_identity("Beacon", "1482")
    r = next(a for a in json.loads(tf.read_text())["agents"] if a["id"] == "beacon")
    assert r["discord"] == "1482" and r["role"] == "Monetization" and r["agent_tier"] == "coordinator"

    # the teammate now resolves (no more <unknown-user>)
    assert team.resolve_discord_user("1482")["name"] == "Beacon"

    # a missing agent (e.g. atlas) is added as a minimal entry
    team.record_bot_identity("Atlas", "1479")
    j = next(a for a in json.loads(tf.read_text())["agents"] if a["id"] == "atlas")
    assert j["discord"] == "1479" and j["name"] == "Atlas"
    assert team.resolve_discord_user("1479")["name"] == "Atlas"

    # idempotent + ignores empties
    team.record_bot_identity("Atlas", "1479")
    team.record_bot_identity("", "x")
    assert len(json.loads(tf.read_text())["agents"]) == 2


def test_roster_block_has_teammate_note(tmp_path, monkeypatch):
    tf = tmp_path / "team.json"
    tf.write_text(json.dumps({"humans": [], "agents": [
        {"id": "beacon", "name": "Beacon", "discord": "1482"}]}))
    monkeypatch.setattr(startup_context, "TEAM_FILE", tf)
    block = startup_context._build_team_summary()
    assert "[discord:1482]" in block and "teammate" in block.lower()
