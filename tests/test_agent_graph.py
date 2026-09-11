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


# --- harness, access and roster cards (from config + runtime overrides) -----

@pytest.fixture
def overlay_cfg(tmp_path, monkeypatch):
    import yaml
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text(yaml.dump({"agents": {
        "atlas": {"tier": "privileged", "privileged": True,
                  "llm": {"provider": "claude_code", "model": "opus"},
                  "extra_dirs": ["/srv/dev"], "description": "Primary agent"},
        "scout": {"tier": "assistant", "llm": {"provider": "claude_code", "model": "sonnet"},
                  "disallow_builtins": ["Bash"], "skills": ["debrief"]},
        "quill": {"tier": "privileged", "llm": {"provider": "codex_cli", "model": "gpt-x",
                                                 "sandbox": "workspace-write"}},
    }}))
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


def test_gather_reads_harness_and_access_from_config(roster, overlay_cfg, monkeypatch):
    _write(roster, [
        {"id": "atlas", "name": "Atlas", "agent_tier": "privileged", "model": "opus"},
        {"id": "scout", "name": "Scout", "agent_tier": "assistant", "model": "sonnet",
         "reports_to": "atlas"},
        {"id": "quill", "name": "Quill", "agent_tier": "privileged", "model": "gpt-x"},
    ])
    monkeypatch.setattr(agent_graph, "_overrides_for", lambda aid: {})
    nodes = {n["id"]: n for n in agent_graph._gather_agents()}

    assert nodes["atlas"]["harness"] == {"provider": "claude_code", "label": "Claude Code",
                                         "model": "opus", "effort": "", "overridden": [],
                                         "sandbox": ""}
    assert nodes["atlas"]["access"]["privileged_scaffold"] is True
    assert nodes["atlas"]["access"]["extra_dirs"] == ["/srv/dev"]
    assert "Full CLI" in nodes["atlas"]["access"]["tier_means"]
    assert nodes["atlas"]["purpose"] == "Primary agent"      # falls back to config description

    assert nodes["scout"]["access"]["denied_builtins"] == ["Bash"]
    assert nodes["scout"]["skills"] == "debrief"
    assert "safe tools only" in nodes["scout"]["access"]["tier_means"]

    assert nodes["quill"]["harness"]["label"] == "Codex CLI"
    assert nodes["quill"]["harness"]["sandbox"] == "workspace-write"


def test_runtime_overrides_win_over_config(roster, overlay_cfg, monkeypatch):
    """agent_config can move an agent to another provider live; the map must
    show what runs now, not what agents.yaml says."""
    _write(roster, [{"id": "scout", "name": "Scout", "agent_tier": "assistant", "model": "sonnet"}])
    monkeypatch.setattr(agent_graph, "_overrides_for",
                        lambda aid: {"provider": "codex_cli", "effort": "high"})
    h = agent_graph._gather_agents()[0]["harness"]
    assert h["provider"] == "codex_cli" and h["label"] == "Codex CLI"
    assert h["model"] == "default of codex_cli"        # no model override: provider default
    assert h["effort"] == "high"
    assert h["overridden"] == ["effort", "provider"]

    monkeypatch.setattr(agent_graph, "_overrides_for", lambda aid: {"model": "gpt-y"})
    h = agent_graph._gather_agents()[0]["harness"]
    assert h["model"] == "gpt-y" and h["provider"] == "claude_code"


def test_gather_without_an_overlay_still_works(roster, monkeypatch):
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)
    monkeypatch.setattr(agent_graph, "_overrides_for", lambda aid: {})
    _write(roster, [{"id": "atlas", "name": "Atlas", "agent_tier": "privileged", "model": "opus"}])
    n = agent_graph._gather_agents()[0]
    assert n["harness"]["label"] == "Claude Code" and n["harness"]["model"] == "opus"
    assert n["access"]["denied_builtins"] == []


def test_render_has_a_roster_card_per_agent(roster, overlay_cfg, monkeypatch):
    _write(roster, [
        {"id": "atlas", "name": "Atlas", "agent_tier": "privileged", "role": "ops", "model": "opus"},
        {"id": "quill", "name": "Quill", "agent_tier": "privileged", "model": "gpt-x",
         "reports_to": "atlas", "rights": ["Read(./**)"]},
    ])
    monkeypatch.setattr(agent_graph, "_overrides_for", lambda aid: {})
    html = agent_graph._render_html(agent_graph._gather_agents(), "atlas", "Agent Map")
    assert 'id="roster"' in html and "Roster" in html
    for word in ("Codex CLI", "Claude Code", "Full CLI", "Read(./**)", "reports to", "Harness"):
        assert word in html
    # still offline: no webfont link, no scripts, no fetch
    assert not re.search(r'(<script[^>]*\ssrc=|<link[^>]*href=|src="https?:|fetch\()', html)


# --- avatars, rights summary, accordion panes ------------------------------

def test_rights_are_summarised_by_what_they_grant():
    s = agent_graph._summarize_rights([
        "Read(./**)", "Write(./**)", "Edit(./**)", "MultiEdit(./**)", "Glob(./**)", "Grep(./**)",
        "Read(//srv/app/**)", "Write(//srv/app/**)",
        "Bash(pnpm:*)", "Bash(npm:*)", "Bash(git:*)",
        "WebSearch(*)", "WebFetch(*)",
        "mcp__kbots-tools", "mcp__hostinger-dns", "mcp__kbots-tools__*",
        "Weird rule",
    ])
    assert s["files"] == ["./**", "//srv/app/**"]          # six verbs, one path each
    assert s["shell"] == ["pnpm", "npm", "git"]
    assert s["web"] == ["WebSearch", "WebFetch"]
    assert s["mcp"] == ["kbots-tools", "hostinger-dns"]     # the __* alias folds in
    assert s["other"] == ["Weird rule"] and s["count"] == 17
    assert agent_graph._summarize_rights(["Bash(*)"])["shell"] == "everything"
    assert agent_graph._summarize_rights([])["files"] == []


def _tiny_png() -> bytes:
    """A valid 2x2 opaque PNG, written by hand so the test needs no Pillow."""
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes([10, 10, 15]) * 2 for _ in range(2))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_avatar_prefers_svg_then_png_then_initial(tmp_path):
    d = tmp_path / "bot"
    d.mkdir()
    # nothing on disk: an initial on the identity mark, still a data URI
    uri = agent_graph._avatar_data_uri(str(d), "Quill", "#ff4444")
    assert uri.startswith("data:image/svg+xml;base64,")
    import base64
    assert ">Q<" in base64.b64decode(uri.split(",", 1)[1]).decode()
    # a png gets inlined (shrunk when Pillow is importable, as-is otherwise)
    (d / "avatar.png").write_bytes(_tiny_png())
    uri = agent_graph._avatar_data_uri(str(d), "Quill", "#ff4444")
    assert uri.startswith("data:image/png;base64,") and len(uri) < 20_000
    # the agent's own svg wins over the png
    (d / "avatar.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    assert agent_graph._avatar_data_uri(str(d), "Quill", "#ff4444").startswith("data:image/svg+xml")
    # no project dir at all
    assert agent_graph._avatar_data_uri("", "", "#888").startswith("data:image/svg+xml")


def test_gather_carries_avatar_and_rights_summary(roster, overlay_cfg, monkeypatch):
    import yaml
    cfg = yaml.safe_load((overlay_cfg / "config" / "agents.yaml").read_text())
    d = overlay_cfg / "agents" / "atlas"
    d.mkdir(parents=True)
    (d / "avatar.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    cfg["agents"]["atlas"]["project_dir"] = str(d)
    (overlay_cfg / "config" / "agents.yaml").write_text(yaml.dump(cfg))
    _write(roster, [{"id": "atlas", "name": "Atlas", "agent_tier": "privileged",
                     "rights": ["Bash(*)", "Read(./**)", "mcp__kbots-tools"]}])
    monkeypatch.setattr(agent_graph, "_overrides_for", lambda aid: {})
    n = agent_graph._gather_agents()[0]
    assert n["avatar"].startswith("data:image/svg+xml;base64,")
    assert n["rights_summary"]["shell"] == "everything"
    assert n["rights_summary"]["files"] == ["./**"] and n["rights_summary"]["mcp"] == ["kbots-tools"]


def test_render_has_accordion_panes_filters_and_avatars(roster, overlay_cfg, monkeypatch):
    _write(roster, [
        {"id": "atlas", "name": "Atlas", "agent_tier": "privileged", "role": "ops", "model": "opus"},
        {"id": "quill", "name": "Quill", "agent_tier": "privileged", "model": "gpt-x", "reports_to": "atlas"},
    ])
    monkeypatch.setattr(agent_graph, "_overrides_for", lambda aid: {})
    html = agent_graph._render_html(agent_graph._gather_agents(), "atlas", "Agent Map")
    for word in ("<details", "<summary>", "Expand all", "Collapse all", 'id="q"', "Codex CLI",
                 "Tools & skills", "Rights", "Schedules", "data:image/svg+xml;base64,", "clipPath"):
        assert word in html, word
    # still offline: no webfont link, no scripts, no fetch; data: URIs are not http
    assert not re.search(r'(<script[^>]*\ssrc=|<link[^>]*href=|src="https?:|fetch\()', html)


def test_png_avatar_is_inlined_without_pillow(tmp_path, monkeypatch):
    """CI has no Pillow; the map must still carry the PNG, unshrunk."""
    import sys
    monkeypatch.setitem(sys.modules, "PIL", None)          # import PIL raises ImportError
    d = tmp_path / "bot"
    d.mkdir()
    (d / "avatar.png").write_bytes(_tiny_png())
    uri = agent_graph._avatar_data_uri(str(d), "Quill", "#ff4444")
    assert uri.startswith("data:image/png;base64,")
    # and one over the size cap falls back to the initial rather than bloating the page
    (d / "avatar.png").write_bytes(_tiny_png() + b"\x00" * (agent_graph._AVATAR_MAX_BYTES + 1))
    assert agent_graph._avatar_data_uri(str(d), "Quill", "#ff4444").startswith("data:image/svg+xml")
