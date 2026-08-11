"""Streaming: tool_use events emit progress; the final result is parsed correctly."""

import json

from src.llm.claude_code import ClaudeCodeProvider, _humanize_tool


def test_humanize_tool():
    assert _humanize_tool("Bash", {"description": "List files", "command": "ls"}) == "List files"
    assert _humanize_tool("Bash", {"command": "arp -a"}) == "arp -a"
    assert _humanize_tool("Bash", {}) == "running a command"
    assert _humanize_tool("mcp__kbots-tools__web_search", {}) == "web search"
    assert _humanize_tool("WebSearch", {}) == "searching the web"
    assert _humanize_tool("Read", {}) == "reading a file"
    assert _humanize_tool("", {}) == ""


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeStderr:
    async def read(self):
        return b""


class _FakeStdin:
    def write(self, b):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class _FakeProc:
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr()
        self.stdin = _FakeStdin()
        self.returncode = None

    async def wait(self):
        self.returncode = 0
        return 0


LINES = [
    b'{"type":"system","subtype":"init"}\n',
    b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash",'
    b'"input":{"description":"List the files","command":"ls"}}]}}\n',
    b'{"type":"assistant","message":{"content":[{"type":"tool_use",'
    b'"name":"mcp__kbots-tools__web_search","input":{"query":"x"}}]}}\n',
    b'{"type":"assistant","message":{"content":[{"type":"text","text":"All done."}]}}\n',
    b'{"type":"result","subtype":"success","result":"All done.","is_error":false,'
    b'"session_id":"s1","usage":{"input_tokens":10,"output_tokens":5}}\n',
]


async def test_stream_emits_progress_and_extracts_result():
    provider = ClaudeCodeProvider(config={})
    progress = []

    async def cb(detail):
        progress.append(detail)

    result_json, stderr = await provider._stream_run(_FakeProc(LINES), "prompt", cb)

    # progress reported for each tool, humanized
    assert progress == ["List the files", "web search"]
    # the final result event is returned and parses like the old json format
    data = json.loads(result_json)
    assert data["result"] == "All done."
    resp = provider._parse_response(result_json)
    assert resp.content == "All done."
    assert resp.session_id == "s1"
    assert resp.tokens_used == 15


async def test_stream_without_callback_is_safe():
    provider = ClaudeCodeProvider(config={})
    result_json, _ = await provider._stream_run(_FakeProc(LINES), "prompt", None)
    assert json.loads(result_json)["result"] == "All done."


async def test_stream_no_result_event_returns_empty():
    provider = ClaudeCodeProvider(config={})
    lines = [b'{"type":"system","subtype":"init"}\n', b'garbage not json\n']
    result_json, _ = await provider._stream_run(_FakeProc(lines), "p", None)
    assert result_json == ""  # caller treats empty as an error
