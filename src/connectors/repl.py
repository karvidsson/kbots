"""Terminal REPL connector — chat with agents from the terminal, no Discord needed.

Used by the dev harness (scripts/dev.py) for local feature testing. Reads lines
from stdin, emits them as IncomingMessages, prints agent replies to stdout.
Type /quit (or Ctrl+D) to shut the engine down gracefully.
"""

import asyncio
import logging
import os
import signal
import threading

from src.core.base import Connector, IncomingMessage

logger = logging.getLogger(__name__)

CYAN = "\033[36m"
GREEN = "\033[32m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


class ReplConnector(Connector):
    """Interactive terminal connector for local development."""
    name = "repl"

    def __init__(self, config: dict, vault=None):
        super().__init__(config, vault)
        self._queue: asyncio.Queue[str] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._prompt_ready = threading.Event()
        self._stopped = False
        self._agent_label = "agent"

    def set_agent_configs(self, agent_configs: dict) -> None:
        """Called by main.py — use the first routed agent's name for the banner."""
        for agent_id, cfg in agent_configs.items():
            if "repl" in cfg.get("routing", {}):
                self._agent_label = cfg.get("display_name", agent_id)
                break

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        print(f"\n{BOLD}{CYAN}── kbots dev chat ──{RESET}")
        print(f"{DIM}Chatting with {self._agent_label}. Type /quit (or Ctrl+D) to exit.{RESET}\n")
        self._prompt_ready.set()
        threading.Thread(target=self._read_stdin, daemon=True, name="repl-stdin").start()
        self._task = asyncio.create_task(self._process_lines(), name="repl-process")

    def _read_stdin(self) -> None:
        """Daemon thread: prompt → read a line → enqueue → wait for the reply."""
        while not self._stopped:
            self._prompt_ready.wait()
            self._prompt_ready.clear()
            if self._stopped:
                break
            try:
                line = input(f"{BOLD}you>{RESET} ")
            except EOFError:
                line = "/quit"
            if self._loop and not self._stopped:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, line)
            if line.strip() == "/quit":
                break

    async def _process_lines(self) -> None:
        while True:
            line = await self._queue.get()
            stripped = line.strip()
            if stripped == "/quit":
                print(f"{DIM}Shutting down...{RESET}")
                os.kill(os.getpid(), signal.SIGINT)
                return
            if not stripped:
                self._prompt_ready.set()
                continue
            try:
                await self.emit(IncomingMessage(
                    connector="repl",
                    channel_id="local",
                    user_id="dev-user",
                    user_name="dev",
                    content=stripped,
                ))
            except Exception as e:
                logger.error(f"REPL message failed: {e}", exc_info=True)
                print(f"{DIM}(error: {e}){RESET}")
            # Reply (if any) has been printed by send() — prompt again
            self._prompt_ready.set()

    async def stop(self) -> None:
        self._stopped = True
        self._prompt_ready.set()
        if self._task:
            self._task.cancel()

    async def send(self, channel_id: str, content: str, **kwargs) -> None:
        print(f"\n{GREEN}{BOLD}{self._agent_label}>{RESET} {content}\n")
