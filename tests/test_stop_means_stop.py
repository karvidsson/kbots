"""/stop and /hold end the turn; the provider must not restart what they killed.

Exit 143 from a killed CLI looked like any other crash, so the retry loop
started a fresh process two seconds later and the agent carried on after the
owner had stopped it.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.core.base import Message, MessageRole
from src.llm.claude_code import ClaudeCodeProvider


class _Pipe:
    def write(self, b):
        pass

    async def drain(self):
        pass

    def close(self):
        pass

    async def readline(self):
        return b""

    async def read(self):
        return b""


class _KilledProc:
    """A CLI that exits as SIGTERM leaves it: no output, exit 143."""

    def __init__(self):
        self.stdin = self.stdout = self.stderr = _Pipe()
        self.returncode = 143

    def kill(self):
        pass

    def terminate(self):
        pass

    async def wait(self):
        return self.returncode


_real_sleep = asyncio.sleep


async def _no_wait(*_):
    await _real_sleep(0)


async def _run(mark_stopped: bool):
    procs = []

    async def spawn(*args, **kwargs):
        proc = _KilledProc()
        procs.append(proc)
        return proc

    def on_proc(proc):
        # Exactly what AgentManager.stop_agent / hold_agent do before killing.
        if mark_stopped:
            proc.kbots_stopped = True

    with tempfile.TemporaryDirectory() as tmp, \
            patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(Path(tmp) / "claude")}), \
            patch("asyncio.create_subprocess_exec", spawn), \
            patch("asyncio.sleep", _no_wait):
        resp = await ClaudeCodeProvider({"timeout": 5}).complete(
            [Message(role=MessageRole.USER, content="hi")],
            project_dir=tmp, proc_callback=on_proc)
    return resp, procs


async def test_a_stopped_turn_is_not_retried_and_posts_nothing():
    resp, procs = await _run(mark_stopped=True)
    assert len(procs) == 1, "a stopped process was restarted"
    assert resp.stop_reason == "aborted" and resp.content == ""


async def test_an_unrequested_crash_is_still_retried():
    resp, procs = await _run(mark_stopped=False)
    assert len(procs) > 1
    assert resp.stop_reason == "error"


async def test_stop_and_hold_mark_the_process_before_killing_it():
    from src.core.agent_manager import AgentManager

    mgr = AgentManager.__new__(AgentManager)
    for method in ("stop_agent", "hold_agent"):
        proc = _KilledProc()
        seen = {}
        proc.terminate = lambda p=proc: seen.setdefault("marked", getattr(p, "kbots_stopped", False))
        mgr._running_procs = {"pica": proc}
        await getattr(mgr, method)("pica")
        assert seen["marked"] is True, method
