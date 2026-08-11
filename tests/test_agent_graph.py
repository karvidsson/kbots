"""Central roster: reconcile_roster syncs+prunes it; agent_graph renders from it."""

import json
import re

import pytest

from src.tools import agent_graph, team


@pytest.fixture
def roster(tmp_path, monkeypatch):
    tf = tmp_path / "team.json"
    monkeypatch.setattr(team, "TEAM_FILE", tf)
    return tf


def _write(tf, agents):
    tf.write_text(json.dumps({"humans": [], "agents": agents}))


# --- reconcile_roster: sync from config, prune stale, preserve curated fields ---

def test_reconcile_prunes_and_enriches(roster, tmp_path):
    sd = tmp_path / "agents" / "scout" / ".claude"
    sd.mkdir(parents=True)
    (sd / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Read(./**)"]}}))
    _write(roster, [
        {"id": "scout", "name": "Scout", "role": "finance", "discord": "999"},  # curated
        {"id": "stale", "name": "Stale"},                                         # not in config
    ])
    config = {"agents": {
        "atlas": {"tier": "privileged", "llm": {"model": "opus"}, "tools": "all",
                   "bot_account": "main", "description": "Primary agent"},
        "scout": {"tier": "assistant", "llm": {"model": "sonnet"}, "description": "Finance bot",
                   "project_dir": str(tmp_path / "agents" / "scout")},
    }}
    team.reconcile_roster(config)

    agents = {a["id"]: a for a in json.loads(roster.read_text())["agents"]}
    assert set(agents) == {"atlas", "scout"}                    # 'stale' pruned
    assert agents["atlas"]["agent_tier"] == "privileged" and agents["atlas"]["model"] == "opus"
    assert agents["atlas"]["role"] == "Primary agent"            # purpose from config description
    assert agents["scout"]["role"] == "finance"                 # curated role preserved over description
    assert agents["scout"]["discord"] == "999"                   # curated preserved
    assert agents["scout"]["rights"] == ["Read(./**)"]           # pulled from settings.json
    assert agents["scout"]["reports_to"] == "atlas"             # hub = the 'main'-account agent


# --- agent_graph reads purely from the roster ---

def test_gather_from_roster(roster):
    _write(roster, [
        {"id": "atlas", "name": "Atlas", "agent_tier": "privileged", "role": "ops",
         "model": "opus", "tools": "all"},
        {"id": "scout", "name": "Scout", "agent_tier": "assistant", "role": "finance",
         "reports_to": "atlas", "model": "sonnet", "rights": ["Read(./**)"]},
    ])
    nodes = {n["id"]: n for n in agent_graph._gather_agents()}
    assert set(nodes) == {"atlas", "scout"}
    assert nodes["atlas"]["tier"] == "privileged" and nodes["atlas"]["model"] == "opus"
    assert nodes["scout"]["purpose"] == "finance" and nodes["scout"]["reports_to"] == "atlas"
    assert nodes["scout"]["rights"] == ["Read(./**)"] and nodes["scout"]["tools"] == "all"


def test_hub_is_reported_to(roster):
    _write(roster, [
        {"id": "atlas", "name": "Atlas", "agent_tier": "privileged"},
        {"id": "scout", "name": "Scout", "agent_tier": "assistant", "reports_to": "atlas"},
    ])
    assert agent_graph._hub_id(agent_graph._gather_agents()) == "atlas"


def test_render_is_self_contained(roster):
    _write(roster, [
        {"id": "atlas", "name": "Atlas", "agent_tier": "privileged",
         "role": "ops", "discord": "1479"},
        {"id": "scout", "name": "Scout", "agent_tier": "assistant", "reports_to": "atlas"},
    ])
    html = agent_graph._render_html(agent_graph._gather_agents(), "atlas", "Agent Map")
    assert not re.search(r'(<script[^>]*\ssrc=|<link[^>]*href=|src="https?:|fetch\()', html)
    assert "<svg" in html and "const NODES" in html and "Scout" in html and "Atlas" in html
    assert "1479" in html and "Discord ID" in html   # discord id carried into the node + panel


async def test_tool_writes_file(roster, tmp_path):
    from src.core.base import ToolContext
    _write(roster, [{"id": "atlas", "name": "Atlas", "agent_tier": "privileged"}])
    ctx = ToolContext(agent_id="atlas", channel_id="c", user_id="u",
                      project_dir=str(tmp_path / "out"))
    out = await agent_graph.agent_graph(ctx, title="My Agents")
    assert ".html" in out
    path = out.split("generated: ")[1].split("\n")[0].strip() if "generated: " in out else None
    if path:
        from pathlib import Path
        assert Path(path).exists() and "<svg" in Path(path).read_text()


def test_gather_includes_enabled_schedules(roster, monkeypatch):
    _write(roster, [{"id": "scout", "name": "Scout", "agent_tier": "assistant"}])
    monkeypatch.setattr(agent_graph.sched, "list_schedules", lambda aid: [
        {"id": "s1", "spec_type": "every", "spec": "3600", "enabled": True, "instruction": "check prices"},
        {"id": "s2", "spec_type": "cron", "spec": "0 8 * * *", "enabled": False, "instruction": "off"},
    ])
    node = {x["id"]: x for x in agent_graph._gather_agents()}["scout"]
    # only the enabled schedule, with humanized timing
    assert node["schedules"] == [{"id": "s1", "timing": "every 60min", "instruction": "check prices"}]
