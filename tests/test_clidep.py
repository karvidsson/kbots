"""CLI-dep resolution with pkgx fallback — resolve branches + tool integration."""

import pytest

from src.core.base import ToolContext
from src.lib import clidep


@pytest.fixture(autouse=True)
def fresh_cache():
    clidep.reset_cache()
    yield
    clidep.reset_cache()


def _fake_which(available: set):
    def which(binary):
        return f"/fake/bin/{binary}" if binary in available else None
    return which


# --- resolve_cli branches ---

def test_on_path_runs_directly(monkeypatch):
    monkeypatch.setattr(clidep.shutil, "which", _fake_which({"ffmpeg"}))
    assert clidep.resolve_cli("ffmpeg") == ["ffmpeg"]


def test_pkgx_fallback(monkeypatch):
    monkeypatch.setattr(clidep.shutil, "which", _fake_which({"pkgx"}))
    assert clidep.resolve_cli("ffmpeg") == ["pkgx", "-q", "ffmpeg"]


def test_neither_returns_none_and_hint(monkeypatch):
    monkeypatch.setattr(clidep.shutil, "which", _fake_which(set()))
    assert clidep.resolve_cli("ffmpeg") is None
    hint = clidep.cli_hint("ffmpeg")
    assert "brew install ffmpeg" in hint and "pkgx.sh" in hint


# --- tool integration ---

def _ctx():
    return ToolContext(agent_id="a", channel_id="c", user_id="u")


async def test_video_missing_ffmpeg_returns_hint(tmp_path, monkeypatch):
    from src.tools import video
    monkeypatch.setattr(video, "resolve_cli", lambda b: None)
    vid = tmp_path / "x.mp4"
    vid.write_bytes(b"fake")
    out = await video.video_frames(_ctx(), str(vid))
    assert "pkgx.sh" in out and "ffmpeg" in out


async def test_video_uses_pkgx_prefix(tmp_path, monkeypatch):
    from src.tools import video
    monkeypatch.setattr(video, "resolve_cli", lambda b: ["pkgx", "-q", "ffmpeg"])
    captured = {}

    class _P:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_shell(cmd, **kw):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr(video.asyncio, "create_subprocess_shell", fake_shell)
    vid = tmp_path / "x.mp4"
    vid.write_bytes(b"fake")
    await video.video_frames(_ctx(), str(vid))
    assert captured["cmd"].startswith("pkgx -q ffmpeg -i ")


async def test_tmux_missing_returns_hint(monkeypatch):
    from src.tools import tmux as tmux_mod
    monkeypatch.setattr(tmux_mod, "resolve_cli", lambda b: None)
    out = await tmux_mod.tmux_list(_ctx())
    assert "pkgx.sh" in out and "tmux" in out          # no longer masked as "No tmux sessions"


async def test_tmux_uses_resolved_prefix(monkeypatch):
    from src.tools import tmux as tmux_mod
    monkeypatch.setattr(tmux_mod, "resolve_cli", lambda b: ["pkgx", "-q", "tmux"])
    captured = {}

    async def fake_run(cmd):
        captured["cmd"] = cmd
        return "sess1: 1 windows"

    monkeypatch.setattr(tmux_mod, "_run", fake_run)
    out = await tmux_mod.tmux_list(_ctx())
    assert captured["cmd"].startswith("pkgx -q tmux list-sessions")
    assert out == "sess1: 1 windows"
