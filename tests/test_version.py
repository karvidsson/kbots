"""Platform version — capture/read, update detection, injection block, tool status."""

import json

from src.core import startup_context, version
from src.core.base import ToolContext
from src.tools.health import platform_version

# --- store roundtrip ---

def test_write_read_roundtrip(tmp_path):
    w = version.write_running_version(tmp_path)
    assert "booted_at" in w and w["short"] == (w["commit"][:8] if w["commit"] else "unknown")
    r = version.read_running_version(tmp_path)
    assert r["commit"] == w["commit"] and r["booted_at"] == w["booted_at"]


def test_read_missing_is_none(tmp_path):
    assert version.read_running_version(tmp_path) is None


def test_current_commit_shape():
    c = version.current_commit()
    assert set(c) == {"commit", "short", "version", "subject", "date"}


# --- semantic version from git describe ---

def test_semver_from_describe(monkeypatch):
    monkeypatch.setattr(version, "_git", lambda *a: "v0.1.0-5-g92768460")
    assert version.semver() == "v0.1.5"   # tag patch 0 + 5 commits


def test_semver_exact_tag(monkeypatch):
    monkeypatch.setattr(version, "_git", lambda *a: "v0.2.0-0-gabc12345")
    assert version.semver() == "v0.2.0"   # on the tag, no distance


def test_semver_patch_tag_plus_distance(monkeypatch):
    monkeypatch.setattr(version, "_git", lambda *a: "v1.3.2-4-gdeadbeef")
    assert version.semver() == "v1.3.6"   # 2 + 4


def test_semver_fallback_no_tags(monkeypatch):
    def fake_git(*a):
        # describe → nothing; rev-parse --short → a sha
        return "" if a and a[0] == "describe" else "92768460"
    monkeypatch.setattr(version, "_git", fake_git)
    assert version.semver() == "g92768460"


# --- update detection (the announce decision) ---

def test_is_update():
    assert version.is_update({"commit": "aaa"}, {"commit": "bbb"}) is True   # changed → announce
    assert version.is_update({"commit": "aaa"}, {"commit": "aaa"}) is False  # same → silent
    assert version.is_update(None, {"commit": "bbb"}) is False               # first boot → silent
    assert version.is_update({"commit": ""}, {"commit": "bbb"}) is False     # no baseline


# --- session-start injection block ---

def test_platform_version_block(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_default_data_dir", lambda: tmp_path)
    (tmp_path / "version.json").write_text(json.dumps(
        {"commit": "abc123", "short": "abc123", "subject": "feat: x", "date": "2026-07-05 10:00"}))
    block = startup_context._build_platform_version()
    assert block.startswith("<platform-version>") and block.endswith("</platform-version>")
    assert "running abc123" in block and "feat: x" in block


def test_platform_version_block_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_default_data_dir", lambda: tmp_path)
    assert startup_context._build_platform_version() is None


# --- the tool: up-to-date vs pending-restart ---

async def test_tool_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_default_data_dir", lambda: tmp_path)
    monkeypatch.setattr(version, "current_commit",
                        lambda: {"commit": "same", "short": "same", "subject": "s", "date": ""})
    (tmp_path / "version.json").write_text(json.dumps(
        {"commit": "same", "short": "same", "subject": "s", "booted_at": 0}))
    out = await platform_version(ToolContext(agent_id="atlas"))
    assert "✅ up to date" in out


async def test_tool_update_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_default_data_dir", lambda: tmp_path)
    monkeypatch.setattr(version, "current_commit",
                        lambda: {"commit": "newone", "short": "newone", "subject": "new", "date": ""})
    (tmp_path / "version.json").write_text(json.dumps(
        {"commit": "oldone", "short": "oldone", "subject": "old", "booted_at": 0}))
    out = await platform_version(ToolContext(agent_id="atlas"))
    assert "⚠️" in out and "NOT running yet" in out and "newone" in out
