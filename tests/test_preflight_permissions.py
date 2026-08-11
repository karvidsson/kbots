"""Rights/permissions preflight — surfaces the failures that silently deny agent tools."""

import os

import pytest

from src.core import preflight


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="root ignores permission bits — chmod 0444 stays writable")
def test_flags_unwritable_claude_json(tmp_path, monkeypatch):
    cfg = tmp_path / "claude-cfg"
    cfg.mkdir()
    (cfg / ".claude.json").write_text("{}")
    (cfg / ".claude.json").chmod(0o444)   # read-only → not writable by the service
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    warns = preflight._check_permissions({"agents": {}})
    assert any("ANY tools" in w and "chown" in w for w in warns)


def test_flags_missing_settings_json(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))     # clean home → no claude.json warning
    ws = tmp_path / "agents" / "scout"
    ws.mkdir(parents=True)                     # workspace exists but no .claude/settings.json
    warns = preflight._check_permissions({"agents": {"scout": {"project_dir": str(ws)}}})
    assert any("scout" in w and "settings.json" in w for w in warns)


def test_clean_setup_has_no_warnings(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude.json").write_text("{}")   # writable, owned by the test user
    ws = tmp_path / "agents" / "scout"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "settings.json").write_text("{}")
    config = {"agents": {"scout": {"project_dir": str(ws)}}}
    assert preflight._check_permissions(config) == []
