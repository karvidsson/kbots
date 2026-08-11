"""_stream_run recovers from an oversized stream-json line instead of aborting the turn."""

import json

from src.llm.claude_code import ClaudeCodeProvider


class _FakeStdin:
    def write(self, b):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class _FakeStdout:
    """Yields the given items in order; an Exception item is raised from readline()."""

    def __init__(self, items):
        self._it = iter(items)

    async def readline(self):
        try:
            nxt = next(self._it)
        except StopIteration:
            return b""
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


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


async def test_recovers_from_oversized_line():
    provider = ClaudeCodeProvider.__new__(ClaudeCodeProvider)
    items = [
        _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        ValueError("Separator is found, but chunk is longer than limit"),   # the overflow
        _line({"type": "result", "result": "final answer"}),
    ]
    result_json, _stderr = await provider._stream_run(_FakeProc(items), "prompt", None)
    assert json.loads(result_json)["result"] == "final answer"   # read past the overflow


async def test_gives_up_after_too_many_oversized_without_raising():
    provider = ClaudeCodeProvider.__new__(ClaudeCodeProvider)
    items = [ValueError("Separator is not found, and chunk exceed the limit")] * 60
    result_json, _stderr = await provider._stream_run(_FakeProc(items), "p", None)
    assert result_json == ""   # no result, but the turn did not crash with the raw error
