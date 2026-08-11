"""scripts/settings.py must share the scaffold's tier allow-lists, not fork them.

A diverged local copy once dropped Write/Edit/MultiEdit — agents created via
the settings TUI silently couldn't write files (headless Claude Code stalls
asking approval per write, which reads as "you haven't granted it yet").
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import settings as settings_tui  # noqa: E402

from src.core import agent_scaffold  # noqa: E402


def test_settings_tui_uses_canonical_allow_lists():
    assert settings_tui.cc_allow_for_tier is agent_scaffold.cc_allow_for_tier


def test_privileged_tier_can_write_files():
    allow = settings_tui.cc_allow_for_tier("privileged")
    for rule in ("Write(*)", "Edit(*)", "MultiEdit(*)"):
        assert rule in allow


def test_mcp_rule_is_server_level_no_wildcard():
    # Claude Code doesn't support wildcards in MCP permission rules —
    # "mcp__server__*" grants nothing. Server-level "mcp__server" is the form.
    for tier in ("privileged", "coordinator", "assistant"):
        allow = agent_scaffold.cc_allow_for_tier(tier)
        assert "mcp__kbots-tools" in allow
        assert not any(r.startswith("mcp__kbots-tools__") for r in allow)


def test_checklist_preserves_rules_outside_master_list(monkeypatch):
    # Scoped tier rules / custom entries must survive a visit to the menu.
    monkeypatch.setattr(settings_tui, "ask", lambda *a, **k: "d")
    current = ["Read(*)", "Write(./**)", "Edit(./**)", "mcp__kbots-tools"]
    result = settings_tui._permission_checklist(current)
    assert set(current) <= set(result)
