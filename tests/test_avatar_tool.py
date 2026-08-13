"""set_agent_avatar tool: composition, token lookup, upload paths (mocked)."""

from pathlib import Path

import pytest

import src.tools.avatar_tools as at
from src.core.base import ToolContext


class StubVault:
    def __init__(self, secrets):
        self._s = secrets

    def get(self, key):
        return self._s.get(key)


def _ctx(vault=None):
    return ToolContext(agent_id="atlas", vault=vault)


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    (tmp_path / "agents" / "milo").mkdir(parents=True)
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


async def test_happy_path_sets_avatar(overlay, monkeypatch):
    monkeypatch.setattr(at, "render_png", lambda svg, png, size: Path(png).write_bytes(b"png") or True)
    monkeypatch.setattr(at, "upload_discord_avatar", lambda png, token: (True, "Discord avatar set"))
    result = await at.set_agent_avatar(
        _ctx(StubVault({"discord-milo": "tok"})), "milo", eyes="ring", accent="green"
    )
    assert "Avatar set on bot 'milo'" in result
    svg = (overlay / "agents" / "milo" / "avatar.svg").read_text()
    assert "eyes=ring" in svg


async def test_missing_token_reports_plainly(overlay, monkeypatch):
    monkeypatch.setattr(at, "render_png", lambda svg, png, size: Path(png).write_bytes(b"png") or True)
    result = await at.set_agent_avatar(_ctx(StubVault({})), "milo")
    assert "no bot token" in result and "manually" in result


async def test_render_failure_reports_plainly(overlay, monkeypatch):
    monkeypatch.setattr(at, "render_png", lambda svg, png, size: False)
    result = await at.set_agent_avatar(_ctx(StubVault({"discord-milo": "t"})), "milo")
    assert "PNG render failed" in result


async def test_bad_style_rejected(overlay):
    result = await at.set_agent_avatar(_ctx(), "milo", eyes="laser")
    assert "Unknown eye style" in result


@pytest.fixture
def overlay_with_primary(tmp_path, monkeypatch):
    """An overlay where the primary agent posts as the shared 'main' account.

    This is the shape every install has: scaffolded agents get an account named
    after themselves, but the first agent runs on 'main'.
    """
    import yaml

    (tmp_path / "agents" / "atlas").mkdir(parents=True)
    (tmp_path / "agents" / "milo").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text(yaml.dump({"agents": {
        "atlas": {"routing": {"discord": {"account": "main"}}},
        "milo": {"routing": {"discord": {"account": "milo"}}},
    }}))
    (tmp_path / "config" / "config.yaml").write_text(yaml.dump({"connectors": {"discord": {
        "accounts": {"main": {"token_key": "discord-token"},
                     "milo": {"token_key": "discord-milo"}},
    }}}))
    monkeypatch.setenv("KBOTS_OVERLAY", str(tmp_path))
    return tmp_path


def test_bot_account_comes_from_agent_routing(overlay_with_primary):
    """Regression: the account was assumed to equal the agent name.

    True for scaffolded agents, false for the primary one — it runs on 'main',
    so the lookup went to a 'discord-atlas' key that was never created.
    """
    assert at._bot_account_for("atlas") == "main"
    assert at._bot_account_for("milo") == "milo"
    # No routing configured at all — keep the old behaviour rather than fail.
    assert at._bot_account_for("stranger") == "stranger"


def test_token_key_follows_the_resolved_account(overlay_with_primary):
    assert at._token_key_for("main") == "discord-token"
    assert at._token_key_for("milo") == "discord-milo"


async def test_primary_agent_avatar_uses_the_main_account_token(
    overlay_with_primary, monkeypatch
):
    """The end-to-end fix: setting the primary agent's avatar must succeed.

    The vault deliberately holds *only* 'discord-token'. Under the old lookup
    this asked for 'discord-atlas', found nothing, and reported 'no bot token
    found' after having already written the files.
    """
    monkeypatch.setattr(at, "render_png", lambda svg, png, size: Path(png).write_bytes(b"png") or True)
    uploaded = {}
    monkeypatch.setattr(at, "upload_discord_avatar",
                        lambda png, token: uploaded.update(token=token) or (True, "ok"))

    result = await at.set_agent_avatar(_ctx(StubVault({"discord-token": "main-tok"})), "atlas")

    assert "Avatar set on bot 'atlas'" in result
    assert uploaded["token"] == "main-tok"


async def test_missing_token_names_the_account_and_key(overlay_with_primary, monkeypatch):
    """The old message named only the agent, which sent you looking for the
    wrong vault key. Say which account and key were actually tried."""
    monkeypatch.setattr(at, "render_png", lambda svg, png, size: Path(png).write_bytes(b"png") or True)
    result = await at.set_agent_avatar(_ctx(StubVault({})), "atlas")
    assert "no bot token" in result
    assert "main" in result and "discord-token" in result


def test_tool_is_discoverable():
    from src.core.tools import get_all_tools
    assert "set_agent_avatar" in get_all_tools()


def test_discord_headers_use_account_token_convention():
    """Regression: per-bot tools looked up discord-token-<bot>, but tokens are
    stored as discord-<bot> — every named-bot Discord tool silently failed."""
    from src.tools.discord_tools import _discord_headers

    headers = _discord_headers(StubVault({"discord-milo": "tok-n"}), bot="milo")
    assert headers and headers["Authorization"] == "Bot tok-n"
    legacy = _discord_headers(StubVault({"discord-token-old": "tok-o"}), bot="old")
    assert legacy and legacy["Authorization"] == "Bot tok-o"
    assert _discord_headers(StubVault({}), bot="ghost") is None


def test_mcp_vault_env_syntax(tmp_path, monkeypatch):
    """'vault:<key>' env values become ${VAR} refs in .mcp.json (no plaintext
    secret on disk) and are reported by vault_env_for_servers for launch-time
    injection. npx servers get -y (headless prompt hang, observed live)."""
    import yaml as _yaml

    from src.core.digest import _build_mcp_json, vault_env_for_servers

    servers = {"svc": {"transport": "stdio", "command": "npx",
                       "args": ["-y", "pkg"], "env": {"TOKEN": "vault:my-secret", "PLAIN": "x"}}}
    out = _build_mcp_json(servers)
    env = out["mcpServers"]["svc"]["env"]
    assert env["TOKEN"] == "${TOKEN}" and env["PLAIN"] == "x"
    assert "my-secret" not in str(out)

    overlay = tmp_path / "ov"
    (overlay / "config").mkdir(parents=True)
    _yaml.dump({"servers": servers}, open(overlay / "config" / "mcp.yaml", "w"))
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    assert vault_env_for_servers() == {"TOKEN": "my-secret"}


def test_add_mcp_server_forces_npx_yes(tmp_path, monkeypatch):
    from src.core.digest import _resolve_mcp_yaml, ingest_mcp_server

    overlay = tmp_path / "ov"
    (overlay / "config").mkdir(parents=True)
    (overlay / "config" / "mcp.yaml").write_text("servers: {}\n")
    monkeypatch.setenv("KBOTS_OVERLAY", str(overlay))
    import yaml as _yaml
    ingest_mcp_server("t", transport="stdio", command="npx", args=["--package=x", "x-mcp"])
    cfg = _yaml.safe_load(open(_resolve_mcp_yaml()))
    assert cfg["servers"]["t"]["args"][0] == "-y"


def test_mcp_json_pins_layer_env_for_kbots_tools(monkeypatch):
    """kbots-tools server env must carry the layer vars — the CLI env
    allowlist doesn't pass them, and without KBOTS_OVERLAY the MCP server
    can't find the vault (regression: 'no Discord token' with token present)."""
    from src.core.digest import _build_mcp_json

    monkeypatch.setenv("KBOTS_OVERLAY", "/ov")
    monkeypatch.setenv("KBOTS_HOME", "/eng")
    servers = {"kbots-tools": {"transport": "stdio", "command": "py"},
               "other": {"transport": "stdio", "command": "npx"}}
    out = _build_mcp_json(servers, agent_env={"KBOTS_BOT_ACCOUNT": "milo"})
    kt = out["mcpServers"]["kbots-tools"]["env"]
    assert kt["KBOTS_OVERLAY"] == "/ov" and kt["KBOTS_HOME"] == "/eng"
    assert kt["KBOTS_BOT_ACCOUNT"] == "milo"
    assert "KBOTS_OVERLAY" not in out["mcpServers"]["other"].get("env", {})
