"""Permission watch: denial detection, dedupe, and escalation delivery."""

import asyncio
import json

from src.core.base import IncomingMessage
from src.core.permission_watch import (
    PermissionWatcher,
    notify,
    scan_stream_event,
    set_watcher,
)
from src.llm.claude_code import ClaudeCodeProvider


def _denial_event(text="Claude requested permissions to write to /x/y.md, "
                       "but you haven't granted it yet."):
    return {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": text},
        ]},
    }


# --- scan_stream_event ----------------------------------------------------

def test_detects_denial_in_string_content():
    assert scan_stream_event(_denial_event())


def test_detects_denial_in_block_content():
    event = {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "is_error": True,
             "content": [{"type": "text",
                          "text": "…but you haven't granted it yet."}]},
        ]},
    }
    assert scan_stream_event(event)


def test_ignores_normal_tool_results_and_other_events():
    ok = {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "content": "file written"},
        ]},
    }
    assert scan_stream_event(ok) == []
    assert scan_stream_event({"type": "assistant", "message": {}}) == []


def test_ignores_explicit_human_rejection():
    # A human saying no at a HITL/permission prompt is not a config failure.
    event = {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "is_error": True,
             "content": "The user doesn't want to proceed with this tool use."},
        ]},
    }
    assert scan_stream_event(event) == []


# --- _stream_run integration ----------------------------------------------

class _FakeStdin:
    def write(self, b): pass
    async def drain(self): pass
    def close(self): pass


class _FakeStdout:
    def __init__(self, items):
        self._it = iter(items)

    async def readline(self):
        try:
            return next(self._it)
        except StopIteration:
            return b""


class _FakeStderr:
    async def read(self):
        return b""


class _FakeProc:
    def __init__(self, items):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(items)
        self.stderr = _FakeStderr()
        self.returncode = 0

    async def wait(self):
        return 0


def _line(obj):
    return json.dumps(obj).encode() + b"\n"


async def test_stream_run_collects_denials():
    provider = ClaudeCodeProvider.__new__(ClaudeCodeProvider)
    items = [
        _line(_denial_event()),
        _line({"type": "result", "result": "done"}),
    ]
    sink: list[str] = []
    await provider._stream_run(_FakeProc(items), "p", None, denial_sink=sink)
    assert len(sink) == 1 and "haven't granted it" in sink[0]


async def test_stream_run_without_sink_still_works():
    provider = ClaudeCodeProvider.__new__(ClaudeCodeProvider)
    items = [_line({"type": "result", "result": "done"})]
    result_json, _ = await provider._stream_run(_FakeProc(items), "p", None)
    assert json.loads(result_json)["result"] == "done"


# --- PermissionWatcher delivery + dedupe ------------------------------------

class _FakeManager:
    def __init__(self):
        self.agent_configs = {"boss": {"routing": {"discord": {"account": "main"}}}}
        self.woken: list[tuple[str, IncomingMessage]] = []

    async def handle_message(self, agent_id, msg):
        self.woken.append((agent_id, msg))


class _FakeAlerter:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, message):
        self.sent.append(message)
        return True


def _config(agent="boss", channel="123"):
    return {"security": {"permission_watch": {
        "agent": agent, "channel": channel, "cooldown": 3600}}}


async def test_report_wakes_configured_agent():
    mgr = _FakeManager()
    w = PermissionWatcher(mgr, _config(), alerter=None)
    w.report("config_unreadable", detail="EACCES", paths=["/h/.claude.json"])
    await asyncio.sleep(0)  # let the delivery task run
    assert len(mgr.woken) == 1
    agent_id, msg = mgr.woken[0]
    assert agent_id == "boss"
    assert msg.channel_id == "123"
    assert "Permission failure" in msg.content
    assert "chown" in msg.content            # exact fix present (POSIX)
    assert "Access required" in msg.content  # SSH-vs-physical guidance present


async def test_report_falls_back_to_alerter_without_agent():
    alerter = _FakeAlerter()
    w = PermissionWatcher(_FakeManager(), _config(agent=""), alerter=alerter)
    w.report("tool_denied", agent_id="worker", detail="denied")
    await asyncio.sleep(0)
    assert len(alerter.sent) == 1 and "worker" in alerter.sent[0]


async def test_cooldown_suppresses_duplicate_reports():
    mgr = _FakeManager()
    w = PermissionWatcher(mgr, _config(), alerter=None)
    for _ in range(5):
        w.report("config_unreadable", detail="EACCES", paths=["/h/.claude.json"])
    await asyncio.sleep(0)
    assert len(mgr.woken) == 1


async def test_distinct_issues_not_suppressed():
    mgr = _FakeManager()
    w = PermissionWatcher(mgr, _config(), alerter=None)
    w.report("config_unreadable", paths=["/h/.claude.json"])
    w.report("tool_denied", agent_id="worker")
    await asyncio.sleep(0)
    assert len(mgr.woken) == 2


async def test_disabled_watcher_reports_nothing():
    mgr = _FakeManager()
    cfg = {"security": {"permission_watch": {"enabled": False, "agent": "boss",
                                             "channel": "123"}}}
    w = PermissionWatcher(mgr, cfg, alerter=None)
    w.report("config_unreadable", paths=["/h/.claude.json"])
    await asyncio.sleep(0)
    assert mgr.woken == []


# --- module-level notify hook -----------------------------------------------

async def test_notify_routes_to_installed_watcher():
    mgr = _FakeManager()
    w = PermissionWatcher(mgr, _config(), alerter=None)
    set_watcher(w)
    try:
        notify("tool_denied", agent_id="worker", detail="x")
        await asyncio.sleep(0)
        assert len(mgr.woken) == 1
    finally:
        set_watcher(None)


def test_notify_without_watcher_is_noop():
    set_watcher(None)
    notify("tool_denied", agent_id="worker")  # must not raise
