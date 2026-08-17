"""codex_cli provider — headless codex exec invocation, resume, MCP translation."""

import json
import stat

import pytest

from src.core.base import Message, MessageRole
from src.llm.codex_cli import CodexCLIProvider, mcp_config_args

# The fake logs argv and signals resume-failure via files next to its own
# binary — NOT env vars, because the provider passes only an allowlisted env to
# the subprocess (a security control), so FAKE_CODEX_* env would be stripped.
FAKE_CODEX = """#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(sys.argv[0]))
with open(os.path.join(here, "argv.log"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if "resume" in sys.argv and os.path.exists(os.path.join(here, "FAIL_RESUME")):
    sys.stderr.write("session not found\\n")
    sys.exit(1)
print(json.dumps({"type": "thread.started", "thread_id": "t-123"}))
print(json.dumps({"type": "item.completed",
                  "item": {"type": "agent_message", "text": "hello from codex"}}))
print(json.dumps({"type": "turn.completed",
                  "usage": {"input_tokens": 10, "output_tokens": 5}}))
"""


@pytest.fixture
def fake_codex(tmp_path):
    bin_path = tmp_path / "fake-codex"
    bin_path.write_text(FAKE_CODEX)
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "argv.log"          # fake writes here (its own directory)
    yield bin_path, log


def _provider(bin_path, **cfg):
    return CodexCLIProvider({"codex_bin": str(bin_path), **cfg})


def _argv(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


async def test_fresh_run_parses_events(fake_codex, tmp_path):
    bin_path, log = fake_codex
    resp = await _provider(bin_path).complete(
        [Message(role=MessageRole.USER, content="hi")],
        project_dir=str(tmp_path / "agent"))
    assert resp.content == "hello from codex"
    assert resp.session_id == "t-123"
    assert resp.tokens_used == 15
    assert resp.stop_reason == "stop"
    argv = _argv(log)[0]
    assert argv[:2] == ["exec", "--json"]
    assert "resume" not in argv
    assert argv[-1] == "hi"


async def test_resume_passes_session_and_last_message_only(fake_codex, tmp_path):
    bin_path, log = fake_codex
    messages = [
        Message(role=MessageRole.USER, content="first"),
        Message(role=MessageRole.ASSISTANT, content="reply"),
        Message(role=MessageRole.USER, content="second"),
    ]
    resp = await _provider(bin_path).complete(
        messages, project_dir=str(tmp_path / "agent"), session_id="t-old")
    assert resp.content == "hello from codex"
    argv = _argv(log)[0]
    i = argv.index("resume")
    assert argv[i + 1] == "t-old"
    assert argv[-1] == "second"  # only the latest user message on resume


async def test_stale_resume_falls_back_to_fresh(fake_codex, tmp_path):
    bin_path, log = fake_codex
    (bin_path.parent / "FAIL_RESUME").write_text("1")
    resp = await _provider(bin_path).complete(
        [Message(role=MessageRole.USER, content="earlier"),
         Message(role=MessageRole.ASSISTANT, content="noted"),
         Message(role=MessageRole.USER, content="hi")],
        project_dir=str(tmp_path / "agent"), session_id="t-dead")
    assert resp.content == "hello from codex"
    assert resp.session_id == "t-123"  # new session from the fresh run
    calls = _argv(log)
    assert "resume" in calls[0] and "resume" not in calls[1]
    # Fresh replay flags the discontinuity
    assert "<session-note>" in calls[1][-1]


async def test_system_message_inlined_on_fresh(fake_codex, tmp_path):
    bin_path, log = fake_codex
    await _provider(bin_path).complete(
        [Message(role=MessageRole.SYSTEM, content="be terse"),
         Message(role=MessageRole.USER, content="hi")],
        project_dir=str(tmp_path / "agent"))
    prompt = _argv(log)[0][-1]
    assert prompt.startswith("<system>\nbe terse\n</system>")
    assert prompt.endswith("hi")


async def test_effort_and_model_flags(fake_codex, tmp_path):
    bin_path, log = fake_codex
    await _provider(bin_path).complete(
        [Message(role=MessageRole.USER, content="hi")],
        project_dir=str(tmp_path / "agent"), model="gpt-5-codex", effort="max")
    argv = _argv(log)[0]
    assert argv[argv.index("-m") + 1] == "gpt-5-codex"
    assert 'model_reasoning_effort = "xhigh"' in argv


def test_invalid_sandbox_rejected(tmp_path):
    with pytest.raises(ValueError, match="sandbox"):
        CodexCLIProvider({"sandbox": "yolo"})


def test_mcp_config_translation(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "kbots-tools": {
                "command": "/x/.venv/bin/python3",
                "args": ["-m", "src.mcp_server"],
                "cwd": "/x/engine",
                "env": {"KBOTS_AGENT_ID": "atlas"},
            }
        }
    }))
    args = mcp_config_args(tmp_path)
    joined = " ".join(args)
    # cwd pin forces a shell wrapper preserving command and args
    assert 'mcp_servers.kbots-tools.command = "/bin/sh"' in joined
    assert "cd '/x/engine' && exec" in joined
    assert 'mcp_servers.kbots-tools.env = {KBOTS_AGENT_ID = "atlas"}' in joined


def test_mcp_config_missing_or_broken(tmp_path):
    assert mcp_config_args(tmp_path) == []
    (tmp_path / ".mcp.json").write_text("{not json")
    assert mcp_config_args(tmp_path) == []
