"""Roster auto-sync (upsert_agent) + delegation-graph injection."""

import json

import pytest

from src.core import startup_context
from src.tools import team


@pytest.fixture
def teamfile(tmp_path, monkeypatch):
    f = tmp_path / "team.json"
    f.write_text(json.dumps({"humans": [], "agents": []}))
    monkeypatch.setattr(team, "TEAM_FILE", f)
    monkeypatch.setattr(startup_context, "TEAM_FILE", f)
    return f


def _agents(f):
    return json.loads(f.read_text())["agents"]


def test_upsert_adds_agent(teamfile):
    added = team.upsert_agent("scout", "Scout", agent_tier="privileged",
                              role="finance", reports_to="atlas")
    assert added is True
    a = _agents(teamfile)
    assert len(a) == 1
    assert a[0]["id"] == "scout" and a[0]["agent_tier"] == "privileged"
    assert a[0]["role"] == "finance" and a[0]["reports_to"] == "atlas"


def test_upsert_is_idempotent(teamfile):
    team.upsert_agent("scout", "Scout", agent_tier="assistant")
    added = team.upsert_agent("scout", "Scout", agent_tier="privileged", role="finance")
    assert added is False                       # updated, not duplicated
    a = _agents(teamfile)
    assert len(a) == 1                          # no duplicate
    assert a[0]["agent_tier"] == "privileged"   # tier updated
    assert a[0]["role"] == "finance"


def test_upsert_preserves_unset_fields(teamfile):
    team.upsert_agent("scout", "Scout", role="finance", reports_to="atlas")
    team.upsert_agent("scout", "Scout")       # no role/reports_to passed
    a = _agents(teamfile)[0]
    assert a["role"] == "finance"               # not wiped
    assert a["reports_to"] == "atlas"


def test_roster_injection_shows_hierarchy(teamfile):
    team.upsert_agent("scout", "Scout", role="finance agent", reports_to="atlas")
    block = startup_context._build_team_summary()
    assert "<team-roster>" in block
    assert "Scout: finance agent" in block
    assert "→ reports to atlas" in block       # delegation edge surfaced
