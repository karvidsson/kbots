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


def test_bot_identity_matches_on_discord_id_not_derived_name(tmp_path, monkeypatch):
    """A bot whose Discord username differs from its config slug must not add a
    second row. Regression: the stub was appended on every startup and
    reconcile_roster pruned it again each boot."""
    tf = tmp_path / "team.json"
    tf.write_text(json.dumps({"humans": [], "agents": [
        {"id": "data-bot", "name": "Data.Bot", "type": "agent",
         "role": "Social media", "agent_tier": "assistant",
         "discord": "1000000000000000001"},
    ]}))
    monkeypatch.setattr(team, "TEAM_FILE", tf)

    team.record_bot_identity("Data Bot", "1000000000000000001")

    agents = json.loads(tf.read_text())["agents"]
    assert len(agents) == 1, f"duplicate stub created: {[a['id'] for a in agents]}"
    assert agents[0]["id"] == "data-bot"
    assert agents[0]["role"] == "Social media"  # untouched


def test_bot_identity_binds_to_slug_match_on_first_run(tmp_path, monkeypatch):
    """The case that actually created the live duplicate.

    A Discord-ID check alone cannot catch this one: on the very first start the
    row has no discord yet, so there is nothing to match on but the name — and
    'Data Bot'.lower() is neither the id 'data-bot' nor the name 'Data.Bot'. The
    row must be found by slug and bound in place, not appended beside.
    """
    tf = tmp_path / "team.json"
    tf.write_text(json.dumps({"humans": [], "agents": [
        {"id": "data-bot", "name": "Data.Bot", "type": "agent",
         "role": "Social media", "agent_tier": "assistant"},   # no discord yet
    ]}))
    monkeypatch.setattr(team, "TEAM_FILE", tf)

    team.record_bot_identity("Data Bot", "1000000000000000001")

    agents = json.loads(tf.read_text())["agents"]
    assert len(agents) == 1, f"duplicate stub created: {[a['id'] for a in agents]}"
    assert agents[0]["discord"] == "1000000000000000001"
    assert agents[0]["agent_tier"] == "assistant"   # bound in place, not replaced


def test_bot_identity_still_adds_a_genuinely_new_agent(tmp_path, monkeypatch):
    """Slug matching must not swallow real additions — and the id it invents
    must be a slug, or it seeds the very mismatch this all guards against."""
    tf = tmp_path / "team.json"
    tf.write_text(json.dumps({"humans": [], "agents": []}))
    monkeypatch.setattr(team, "TEAM_FILE", tf)

    team.record_bot_identity("Brand New", "999")

    agents = json.loads(tf.read_text())["agents"]
    assert len(agents) == 1
    assert agents[0]["id"] == "brand-new"
    assert agents[0]["discord"] == "999"


async def test_team_update_refuses_duplicate_discord_binding(tmp_path, monkeypatch):
    """One Discord app per agent: binding a held ID to a second row is refused.

    Deliberately `async def` rather than asyncio.run(): the suite runs
    asyncio_mode="auto", so async tests get a fresh loop each and never share
    loop state across tests. Driving a coroutine by hand from a sync test
    instead is what made this suite order-dependent under Python 3.14.
    """
    from types import SimpleNamespace

    tf = tmp_path / "team.json"
    tf.write_text(json.dumps({"humans": [], "agents": [
        {"id": "data-bot", "name": "Data.Bot", "type": "agent", "discord": "2002"},
        {"id": "data bot", "name": "Data Bot", "type": "agent"},
    ]}))
    monkeypatch.setattr(team, "TEAM_FILE", tf)

    fn = getattr(team.team_update, "fn", team.team_update)
    out = await fn(SimpleNamespace(agent_manager=None),
                   id="data bot", field="discord", value="2002")

    assert "already bound" in out
    rows = json.loads(tf.read_text())["agents"]
    assert [r.get("discord") for r in rows] == ["2002", None]


def test_roster_block_has_teammate_note(tmp_path, monkeypatch):
    tf = tmp_path / "team.json"
    tf.write_text(json.dumps({"humans": [], "agents": [
        {"id": "beacon", "name": "Beacon", "discord": "1482"}]}))
    monkeypatch.setattr(startup_context, "TEAM_FILE", tf)
    block = startup_context._build_team_summary()
    assert "[discord:1482]" in block and "teammate" in block.lower()
