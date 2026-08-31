"""The resume deadline times session LOAD, not the turn.

The old code capped a resumed turn at 600s end to end, so a resumed turn that
merely ran long was killed and logged as a hang. These tests pin the two apart:
a resumed session that comes up and then works past the deadline must survive,
and one that never comes up at all must still be dropped.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.base import Message, MessageRole
from src.llm.claude_code import ClaudeCodeProvider, _StartupTimeoutError


def _line(obj):
    return json.dumps(obj).encode() + b"\n"


_INIT = _line({"type": "system", "subtype": "init"})
_RESULT = _line({"type": "result", "result": "final answer", "session_id": "s1"})


class _FakeStdin:
    def write(self, b):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class _FakeStdout:
    """Yields (delay, bytes) pairs, sleeping before each line."""

    def __init__(self, items):
        self._it = iter(items)

    async def readline(self):
        try:
            delay, data = next(self._it)
        except StopIteration:
            return b""
        if delay:
            await asyncio.sleep(delay)
        return data


class _FakeStderr:
    async def read(self):
        return b""


class _FakeProc:
    def __init__(self, items):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(items)
        self.stderr = _FakeStderr()
        self.returncode = 0
        self.killed = False

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


# --- the liveness signal itself ---

async def test_stream_run_signals_on_first_line():
    provider = ClaudeCodeProvider.__new__(ClaudeCodeProvider)
    started = asyncio.Event()
    proc = _FakeProc([(0, _INIT), (0, _RESULT)])
    await provider._stream_run(proc, "p", None, started=started)
    assert started.is_set()


async def test_stream_run_signals_even_on_unparseable_line():
    """An event we cannot parse still proves the CLI is up. Requiring valid JSON
    would kill a working session over an unrecognised event type."""
    provider = ClaudeCodeProvider.__new__(ClaudeCodeProvider)
    started = asyncio.Event()
    proc = _FakeProc([(0, b"not json at all\n"), (0, _RESULT)])
    await provider._stream_run(proc, "p", None, started=started)
    assert started.is_set()


# --- the deadline ---

async def test_await_startup_returns_once_signalled():
    started = asyncio.Event()

    async def _run():
        started.set()
        await asyncio.sleep(5)
        return ("", "")

    task = asyncio.ensure_future(_run())
    await asyncio.wait_for(
        ClaudeCodeProvider._await_startup(started, task, 1.0), timeout=1.0)
    task.cancel()


async def test_await_startup_raises_when_nothing_arrives():
    started = asyncio.Event()

    async def _run():
        await asyncio.sleep(5)
        return ("", "")

    task = asyncio.ensure_future(_run())
    with pytest.raises(_StartupTimeoutError):
        await ClaudeCodeProvider._await_startup(started, task, 0.05)
    task.cancel()


async def test_await_startup_returns_when_run_finishes_first():
    """A process that exits before emitting anything (dead session, auth error)
    has answered the liveness question. Holding it for the full deadline would
    stall the caller's own error handling behind a ten-minute wait."""
    started = asyncio.Event()

    async def _run():
        return ("", "boom")

    task = asyncio.ensure_future(_run())
    await asyncio.wait_for(
        ClaudeCodeProvider._await_startup(started, task, 30), timeout=1.0)


# --- end to end through complete() ---

def _session_dir(home: Path, cwd: Path, session_id: str) -> None:
    slug = str(cwd).replace("/", "-")
    f = home / "projects" / slug / f"{session_id}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("")


async def _complete_with(items, *, startup, timeout):
    """Run complete() against a faked CLI, resuming a session that exists."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "claude"
        cwd = Path(tmp) / "agent"
        cwd.mkdir()
        home.mkdir()
        _session_dir(home, cwd.resolve(), "sess-1")
        procs = []

        async def _spawn(*args, **kwargs):
            proc = _FakeProc(items.pop(0) if items else [])
            procs.append(proc)
            return proc

        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(home)}), \
                patch("asyncio.create_subprocess_exec", _spawn):
            provider = ClaudeCodeProvider(
                {"timeout": timeout, "resume_startup_timeout": startup})
            resp = await provider.complete(
                [Message(role=MessageRole.USER, content="hi")],
                project_dir=str(cwd), session_id="sess-1")
        return resp, procs


async def test_slow_resumed_turn_survives_past_the_startup_deadline():
    """THE REGRESSION. Comes up immediately, then works for well over the
    startup deadline. Under the old whole-turn cap this was killed and logged
    as a hang; it must now run to completion on the first attempt."""
    resp, procs = await _complete_with(
        [[(0, _INIT), (0.3, _RESULT)]], startup=0.05, timeout=10)
    assert resp.content == "final answer"
    assert len(procs) == 1, "should not have retried"
    assert not procs[0].killed


async def test_resume_that_never_starts_is_dropped_and_retried_fresh():
    """The genuine stale-resume hang still gets caught: no first event before
    the deadline, so --resume is dropped and the retry starts fresh."""
    resp, procs = await _complete_with(
        [[(5, _INIT)], [(0, _INIT), (0, _RESULT)]], startup=0.05, timeout=10)
    assert resp.content == "final answer"
    assert len(procs) == 2, "should have retried once"
    assert procs[0].killed, "the hung process must be killed"
