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
    assert 'approval_policy = "on-request"' in argv
    assert 'approvals_reviewer = "auto_review"' in argv
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


async def test_per_agent_execution_policy_and_directories(fake_codex, tmp_path,
                                                          monkeypatch):
    bin_path, log = fake_codex
    shared = tmp_path / "shared"
    repo = tmp_path / "repo"
    shared.mkdir()
    repo.mkdir()
    monkeypatch.setenv("KBOTS_TMP", str(shared))
    monkeypatch.delenv("KBOTS_OVERLAY", raising=False)

    await _provider(bin_path, sandbox="read-only").complete(
        [Message(role=MessageRole.USER, content="hi")],
        project_dir=str(tmp_path / "agent"),
        sandbox="danger-full-access",
        approval_policy="never",
        approvals_reviewer="user",
        extra_dirs=[str(repo)],
        sandbox_dirs=[str(repo)],
    )

    argv = _argv(log)[0]
    assert argv[argv.index("-s") + 1] == "danger-full-access"
    assert 'approval_policy = "never"' in argv
    assert 'approvals_reviewer = "user"' in argv
    assert argv.count("--add-dir") == 2
    assert str(shared) in argv
    assert str(repo) in argv


def test_invalid_sandbox_rejected(tmp_path):
    with pytest.raises(ValueError, match="sandbox"):
        CodexCLIProvider({"sandbox": "yolo"})


@pytest.mark.parametrize("key,value,match", [
    ("approval_policy", "sometimes", "approval policy"),
    ("approvals_reviewer", "nobody", "approvals reviewer"),
])
def test_invalid_approval_config_rejected(key, value, match):
    with pytest.raises(ValueError, match=match):
        CodexCLIProvider({key: value})


async def test_invalid_per_agent_sandbox_rejected(fake_codex, tmp_path):
    bin_path, _ = fake_codex
    with pytest.raises(ValueError, match="sandbox"):
        await _provider(bin_path).complete(
            [Message(role=MessageRole.USER, content="hi")],
            project_dir=str(tmp_path / "agent"), sandbox="yolo")


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


def _write_mcp(tmp_path, env):
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"kbots-tools": {"command": "/x/py", "env": env}}
    }))


def test_mcp_env_carries_loopback_api(tmp_path):
    """Codex builds each server's env from this table alone — it does not
    forward its own. Without the loopback vars every inter-agent tool inside
    the MCP server reports 'no agent manager available'."""
    _write_mcp(tmp_path, {"KBOTS_AGENT_ID": "atlas"})
    joined = " ".join(mcp_config_args(tmp_path, {
        "KBOTS_INTERNAL_API": "http://127.0.0.1:5151",
        "KBOTS_INTERNAL_TOKEN": "tok-abc",
        "GH_TOKEN": "ghp_secret",
    }))
    assert 'KBOTS_INTERNAL_API = "http://127.0.0.1:5151"' in joined
    assert 'KBOTS_INTERNAL_TOKEN = "tok-abc"' in joined
    assert 'KBOTS_AGENT_ID = "atlas"' in joined
    # Only the loopback pair is copied through — not every secret codex holds.
    assert "ghp_secret" not in joined


def test_mcp_env_expands_variable_refs(tmp_path):
    """Claude Code expands ${VAR} in .mcp.json; codex does not, so an
    unexpanded ref would reach the server as its own literal text and fail as
    a bad credential rather than a missing one."""
    _write_mcp(tmp_path, {"TOKEN": "${HOSTINGER_API_TOKEN}",
                          "PROFILE": "${KBOTS_PROFILE:-none}"})
    joined = " ".join(mcp_config_args(
        tmp_path, {"HOSTINGER_API_TOKEN": "hpk-1"}))
    assert 'TOKEN = "hpk-1"' in joined
    assert 'PROFILE = "none"' in joined   # unset -> fallback
    assert "${" not in joined


def test_mcp_env_omitted_when_nothing_to_set(tmp_path):
    _write_mcp(tmp_path, {})
    assert "env" not in " ".join(mcp_config_args(tmp_path, {}))


async def test_run_passes_loopback_env_to_mcp_servers(fake_codex, tmp_path):
    """End to end through complete(): extra_env reaches the server table."""
    bin_path, log = fake_codex
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    _write_mcp(agent_dir, {"KBOTS_AGENT_ID": "atlas"})
    await _provider(bin_path).complete(
        [Message(role=MessageRole.USER, content="hi")],
        project_dir=str(agent_dir),
        extra_env={"KBOTS_INTERNAL_API": "http://127.0.0.1:9", "KBOTS_INTERNAL_TOKEN": "t"},
    )
    assert 'KBOTS_INTERNAL_TOKEN = "t"' in " ".join(_argv(log)[0])


def test_mcp_env_carries_sender_identity(tmp_path):
    """The MCP server derives ToolContext.user_id from this table, and the
    admin gate on agent_config/set_hitl reads it. Dropped, the owner's own
    call is refused."""
    _write_mcp(tmp_path, {"KBOTS_AGENT_ID": "atlas"})
    joined = " ".join(mcp_config_args(tmp_path, {"KBOTS_USER_ID": "12345"}))
    assert 'KBOTS_USER_ID = "12345"' in joined


async def test_run_passes_user_id_to_mcp_servers(fake_codex, tmp_path):
    """End to end: the user_id kwarg the engine passes reaches the server."""
    bin_path, log = fake_codex
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    _write_mcp(agent_dir, {"KBOTS_AGENT_ID": "atlas"})
    await _provider(bin_path).complete(
        [Message(role=MessageRole.USER, content="hi")],
        project_dir=str(agent_dir),
        user_id="99887766",
    )
    assert 'KBOTS_USER_ID = "99887766"' in " ".join(_argv(log)[0])


async def test_no_user_id_leaves_identity_unset(fake_codex, tmp_path):
    """Scheduler/trigger/agent-to-agent turns have no sender. The gate must
    fail closed rather than inherit whoever ran last."""
    bin_path, log = fake_codex
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    _write_mcp(agent_dir, {"KBOTS_AGENT_ID": "atlas"})
    await _provider(bin_path).complete(
        [Message(role=MessageRole.USER, content="hi")],
        project_dir=str(agent_dir),
    )
    assert "KBOTS_USER_ID" not in " ".join(_argv(log)[0])


# --- deadlines: same two questions as claude_code, same defaults ---

SLOW_CODEX = """#!/usr/bin/env python3
import json, os, sys, time
here = os.path.dirname(os.path.abspath(sys.argv[0]))
if os.path.exists(os.path.join(here, "SILENT")):
    time.sleep(3)                       # never emits an event
else:
    print(json.dumps({"type": "thread.started", "thread_id": "t-1"}), flush=True)
    time.sleep(float(open(os.path.join(here, "DELAY")).read())
               if os.path.exists(os.path.join(here, "DELAY")) else 0)
    print(json.dumps({"type": "item.completed",
                      "item": {"type": "agent_message", "text": "done"}}))
"""


@pytest.fixture
def slow_codex(tmp_path):
    bin_path = tmp_path / "slow-codex"
    bin_path.write_text(SLOW_CODEX)
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)
    return bin_path


def test_turn_timeout_defaults_to_an_hour_not_ten_minutes():
    """600s was a whole-turn cap here. A long turn was killed and reported as
    a timeout with no partial output, which is what claude_code fixed by
    splitting liveness from work."""
    p = CodexCLIProvider({})
    assert p._timeout == 3600
    assert p._startup_timeout == 600
    assert CodexCLIProvider({"timeout": 90})._timeout == 90


async def test_per_call_timeout_wins(slow_codex, tmp_path):
    """The reflector asks for a cheap 180s. That kwarg was ignored on codex."""
    (tmp_path / "DELAY").write_text("5")
    with pytest.raises(RuntimeError, match="timed out after 1s"):
        await _provider(slow_codex, timeout=3600).complete(
            [Message(role=MessageRole.USER, content="hi")],
            project_dir=str(tmp_path / "agent"), timeout=1)


async def test_a_long_fresh_turn_is_not_killed_by_the_startup_deadline(
        slow_codex, tmp_path):
    """A fresh session gets no liveness deadline: nothing about it can fail by
    never coming up, so a slow turn must simply be allowed to be slow."""
    (tmp_path / "DELAY").write_text("2")
    resp = await _provider(slow_codex, resume_startup_timeout=1).complete(
        [Message(role=MessageRole.USER, content="hi")],
        project_dir=str(tmp_path / "agent"))
    assert resp.content == "done"


async def test_a_resume_that_never_starts_falls_back_to_fresh(
        slow_codex, tmp_path):
    """Only a resume can fail by never coming up, and the answer is to drop
    the resume rather than to fail the turn."""
    (tmp_path / "SILENT").write_text("")
    provider = _provider(slow_codex, resume_startup_timeout=1)
    with pytest.raises(RuntimeError, match="codex exec failed"):
        # both attempts are silent here, so it exhausts the retry — the point
        # is that it RETRIED rather than raising a timeout after an hour
        await provider.complete(
            [Message(role=MessageRole.USER, content="hi")],
            project_dir=str(tmp_path / "agent"), session_id="old-thread")


async def test_subprocess_stdin_is_not_inherited(fake_codex, tmp_path):
    """`codex exec` reads stdin on every run. An inherited stdin that never
    reaches EOF blocks the turn until the deadline; it is /dev/null under
    launchd by luck, not design."""
    import asyncio as _asyncio
    seen = {}
    real = _asyncio.create_subprocess_exec

    async def _spy(*a, **kw):
        seen.update(kw)
        return await real(*a, **kw)

    bin_path, _ = fake_codex
    import src.llm.codex_cli as mod
    orig = mod.asyncio.create_subprocess_exec
    mod.asyncio.create_subprocess_exec = _spy
    try:
        await _provider(bin_path).complete(
            [Message(role=MessageRole.USER, content="hi")],
            project_dir=str(tmp_path / "agent"))
    finally:
        mod.asyncio.create_subprocess_exec = orig
    assert seen["stdin"] == _asyncio.subprocess.DEVNULL
