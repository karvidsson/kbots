"""Workspace trust survives a non-writable ~/.claude.json (e.g. root-owned)."""

import json
import logging
import os
import stat

import pytest

from src.llm import claude_code


def test_trust_marked_when_claude_json_not_writable(tmp_path, monkeypatch):
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({"projects": {}}))
    cfg.chmod(0o444)   # read-only target: a plain write would EACCES; atomic rename still works
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    claude_code._trusted_dirs.clear()

    ws = tmp_path / "agents" / "scout"
    ws.mkdir(parents=True)
    claude_code._ensure_workspace_trusted(ws)

    data = json.loads(cfg.read_text())
    assert data["projects"][str(ws)]["hasTrustDialogAccepted"] is True   # trusted despite RO target
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600                   # perms stay secret (not 0644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads 0o000 regardless — the failure cannot occur")
def test_unreadable_claude_json_fails_loudly_without_clobbering(tmp_path, monkeypatch, caplog):
    """0o444 above is read-ONLY; the real-world root-owned file is 0o600 — unreadable.

    That fails at read_text(), before the atomic write ever runs, so the
    read-only test does not cover it. Must warn loudly and leave the file alone.
    """
    cfg = tmp_path / ".claude.json"
    original = json.dumps({"projects": {}, "oauthAccount": {"must": "survive"}})
    cfg.write_text(original)
    cfg.chmod(0o000)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    claude_code._trusted_dirs.clear()

    ws = tmp_path / "agents" / "scout"
    ws.mkdir(parents=True)
    with caplog.at_level(logging.ERROR):
        claude_code._ensure_workspace_trusted(ws)   # must not raise

    assert "EVERY TOOL will be denied" in caplog.text   # not a warning lost in the noise
    assert "chown" in caplog.text                       # tells the operator what to do

    cfg.chmod(0o600)
    assert json.loads(cfg.read_text()) == json.loads(original)   # credentials intact
    assert str(ws) not in claude_code._trusted_dirs              # not cached as trusted


def test_trust_creates_file_and_caches(tmp_path, monkeypatch):
    cfg = tmp_path / ".claude.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    claude_code._trusted_dirs.clear()

    ws = tmp_path / "agents" / "eng"
    ws.mkdir(parents=True)
    claude_code._ensure_workspace_trusted(ws)          # no file yet → creates it
    assert str(ws) in claude_code._trusted_dirs         # cached per-process
    assert json.loads(cfg.read_text())["projects"][str(ws)]["hasTrustDialogAccepted"] is True
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600


def test_trust_reheals_after_concurrent_clobber(tmp_path, monkeypatch):
    """A concurrent CLI rewrite of ~/.claude.json drops our entry; the next
    spawn must detect it from disk and re-trust despite the warm process cache."""
    cfg = tmp_path / ".claude.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    claude_code._trusted_dirs.clear()

    ws = tmp_path / "agents" / "eng"
    ws.mkdir(parents=True)
    claude_code._ensure_workspace_trusted(ws)
    assert json.loads(cfg.read_text())["projects"][str(ws)]["hasTrustDialogAccepted"] is True

    # Another Claude CLI process rewrites the file from its own stale snapshot
    cfg.write_text(json.dumps({"projects": {"/somewhere/else": {}}}))

    claude_code._ensure_workspace_trusted(ws)   # same process, cache is warm
    data = json.loads(cfg.read_text())
    assert data["projects"][str(ws)]["hasTrustDialogAccepted"] is True   # healed
    assert "/somewhere/else" in data["projects"]                          # merged, not clobbered back
