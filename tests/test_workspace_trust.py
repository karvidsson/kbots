"""Agent workspaces must be marked trusted so Claude Code honors their
settings.json permissions (Bash, MCP tools) in headless mode."""

import json

from src.llm.claude_code import _ensure_workspace_trusted, _trusted_dirs


def test_trust_written_to_claude_json(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfgdir"
    cfg_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    _trusted_dirs.clear()

    ws = tmp_path / "agents" / "main"
    ws.mkdir(parents=True)
    _ensure_workspace_trusted(ws)

    data = json.loads((cfg_dir / ".claude.json").read_text())
    assert data["projects"][str(ws)]["hasTrustDialogAccepted"] is True


def test_preserves_existing_claude_json(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfgdir"
    cfg_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    _trusted_dirs.clear()
    # Pre-existing config with other content must survive
    (cfg_dir / ".claude.json").write_text(json.dumps({
        "numStartups": 5,
        "projects": {"/other/dir": {"hasTrustDialogAccepted": True, "x": 1}},
    }))

    ws = tmp_path / "agents" / "helper"
    ws.mkdir(parents=True)
    _ensure_workspace_trusted(ws)

    data = json.loads((cfg_dir / ".claude.json").read_text())
    assert data["numStartups"] == 5
    assert data["projects"]["/other/dir"] == {"hasTrustDialogAccepted": True, "x": 1}
    assert data["projects"][str(ws)]["hasTrustDialogAccepted"] is True


def test_lost_entry_reheals_despite_cache(tmp_path, monkeypatch):
    """The per-process cache must NOT make trust write-once: concurrent CLI
    processes rewrite ~/.claude.json and can drop our entry (production
    incident 2026-07-13 — all agents ran sandboxed until restart). Every call
    verifies against disk and re-trusts when the entry is gone."""
    cfg_dir = tmp_path / "cfgdir"
    cfg_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    _trusted_dirs.clear()

    ws = tmp_path / "agents" / "a"
    ws.mkdir(parents=True)
    _ensure_workspace_trusted(ws)
    (cfg_dir / ".claude.json").unlink()   # simulate a concurrent rewrite losing it
    _ensure_workspace_trusted(ws)         # cache is warm — must still re-check disk
    data = json.loads((cfg_dir / ".claude.json").read_text())
    assert data["projects"][str(ws)]["hasTrustDialogAccepted"] is True
