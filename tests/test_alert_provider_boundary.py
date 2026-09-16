"""Exercise actual subprocess adapters against offline instrumented CLI doubles."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.core.base import Message, MessageRole
from src.llm.claude_code import ClaudeCodeProvider
from src.llm.codex_cli import CodexCLIProvider
from src.llm.openai_compat import OpenAICompatProvider

FAKE = """#!/usr/bin/env python3
import json, os, pathlib, sys
here = pathlib.Path(__file__).parent
(here / "arguments.json").write_text(json.dumps(sys.argv[1:]))
(here / "environment.json").write_text(json.dumps({k:v for k,v in os.environ.items() if k.startswith("KBOTS_")}))
if "--print" in sys.argv:
    sys.stdin.read()
    print(json.dumps({"type":"system", "subtype":"init"}))
    print(json.dumps({"type":"result", "result":"Diagnosis from supplied evidence", "session_id":"test"}))
else:
    print(json.dumps({"type":"thread.started", "thread_id":"test"}))
    print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"Diagnosis"}}))
    print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":1,"output_tokens":1}}))
"""


@pytest.fixture
def cli(tmp_path, monkeypatch):
    path = tmp_path / "fake-cli"
    path.write_text(FAKE)
    path.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    return path


@pytest.mark.parametrize("kind", ["claude", "codex"])
async def test_no_tools_no_session_no_secret_env_or_extra_directories(cli, tmp_path, kind):
    provider = (
        ClaudeCodeProvider({"claude_bin": str(cli)}) if kind == "claude" else CodexCLIProvider({"codex_bin": str(cli)})
    )
    response = await provider.complete(
        [Message(role=MessageRole.USER, content="Diagnose synthetic issue")],
        tool_free=True,
        project_dir=str(tmp_path / "fresh"),
        extra_dirs=["/must-not-grant"],
        extra_env={"KBOTS_INTERNAL_TOKEN": "forbidden"},
        allowed_tools=["Bash"],
    )
    assert response.content
    args = json.loads((tmp_path / "arguments.json").read_text())
    env = json.loads((tmp_path / "environment.json").read_text())
    assert "--resume" not in args and "resume" not in args
    assert "--add-dir" not in args and "--allowedTools" not in args
    assert "KBOTS_INTERNAL_TOKEN" not in env
    if kind == "claude":
        assert args[args.index("--tools") + 1] == ""
        assert args[args.index("--mcp-config") + 1] == '{"mcpServers":{}}'
        assert "--strict-mcp-config" in args and "--no-session-persistence" in args
        assert args[args.index("--settings") + 1] == '{"disableAllHooks":true}'
        assert not (tmp_path / ".claude" / ".claude.json").exists()
    else:
        assert args[args.index("-s") + 1] == "read-only"
        assert 'approval_policy = "never"' in args
        assert "--dangerously-bypass-hook-trust" in args
        assert env["KBOTS_DENIED_TOOLS"] == "*"


@pytest.mark.parametrize("kind", ["claude", "codex"])
async def test_existing_session_is_rejected_before_subprocess(cli, tmp_path, kind):
    provider = (
        ClaudeCodeProvider({"claude_bin": str(cli)}) if kind == "claude" else CodexCLIProvider({"codex_bin": str(cli)})
    )
    with pytest.raises(ValueError, match="cannot resume"):
        await provider.complete(
            [], tool_free=True, session_id="old-privileged-session", project_dir=str(tmp_path / "fresh")
        )
    assert not (tmp_path / "arguments.json").exists()


async def test_global_codex_servers_and_plugins_are_disabled_before_start(cli, tmp_path):
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    original = '[mcp_servers.remote]\ncommand="must-not-start"\n[plugins."test@local"]\nenabled=true\n'
    config.write_text(original)
    await CodexCLIProvider({"codex_bin": str(cli)}).complete([], tool_free=True, project_dir=str(tmp_path / "fresh"))
    args = json.loads((tmp_path / "arguments.json").read_text())
    assert 'mcp_servers."remote".enabled=false' in args
    assert 'plugins."test@local".enabled=false' in args
    assert config.read_text() == original


@pytest.mark.parametrize("tool", ["Bash", "Edit", "Read", "mcp__remote__send", "web_search", "future_tool"])
def test_codex_deny_hook_refuses_all_tool_names(tool):
    hook = Path(__file__).parents[1] / "src" / "llm" / "codex_hook_deny.py"
    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"tool_name": tool}),
        env={**os.environ, "KBOTS_DENIED_TOOLS": "*"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("extra", [{"session_id": "old-session"}, {"tools": [object()]}])
async def test_http_provider_rejects_tools_and_resume_before_any_io(extra):
    provider = OpenAICompatProvider({})
    provider._resolve_base_url = AsyncMock(side_effect=AssertionError("must not access endpoint"))
    with pytest.raises(ValueError, match="cannot resume or receive tools"):
        await provider.complete([], tool_free=True, **extra)
    provider._resolve_base_url.assert_not_awaited()


@pytest.mark.parametrize(
    "message",
    [
        Message(role=MessageRole.TOOL, content="prior tool result"),
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[{"name": "shell", "arguments": "{}"}]),
    ],
)
async def test_http_provider_rejects_tool_history_before_any_io(message):
    provider = OpenAICompatProvider({})
    provider._resolve_base_url = AsyncMock(side_effect=AssertionError("must not access endpoint"))
    with pytest.raises(ValueError, match="cannot receive tool history"):
        await provider.complete([message], tool_free=True)
    provider._resolve_base_url.assert_not_awaited()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("unexpected_tools", [False, True])
async def test_http_provider_tool_free_request_and_response_contract(native, unexpected_tools):
    provider = OpenAICompatProvider({"local": {"base_url": "http://localhost:11434/v1", "model": "test"}})
    provider._ollama_native = native
    msg = {"content": "Diagnosis from supplied evidence"}
    if unexpected_tools:
        msg["tool_calls"] = [{"function": {"name": "shell", "arguments": {"command": "must not run"}}}]
    request = AsyncMock(return_value={"message": msg} if native else {"choices": [{"message": msg}]})
    if native:
        provider._request_native = request
    else:
        provider._request = request
    response = await provider.complete(
        [Message(role=MessageRole.USER, content="Synthetic incident")], tool_free=True, session_id=None
    )
    payload = request.call_args.args[1]
    assert "tools" not in payload and "session_id" not in payload
    assert not response.tool_calls
    assert response.stop_reason == ("error" if unexpected_tools else "end")
    assert "must not run" not in response.content
