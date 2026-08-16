"""Browser janitor — quits the shared debug Chrome once it has sat idle.

Agents drive one real Chrome (src/tools/chrome_desktop.py) over CDP and leave
it running when their task ends. Tabs full of video keep decoding and
compositing around the clock — days of that cooked a laptop at ~50% load
(2026-08-14). Nothing else ever closes that browser, so this background task
(sibling of heartbeat/reflector in main.py) does.

Activity signal: the cross-process `chrome_browser` reservation
(src/core/tool_reservation.py). A live reservation means an agent is driving
right now — never touch the browser then. The record's `last_used_at` (kept
even after expiry) marks the last drive; a runtime_state flag carries that
high-water mark across restarts and covers the record being replaced.

Quitting is graceful and lossless: Browser.close over CDP (SIGTERM to the
main Chrome process as fallback), profile/cookies/logins persist on disk, and
the next chrome_browser call relaunches Chrome via scripts/chrome-debug.sh.
"""

import asyncio
import logging
import os
import socket
import time
from pathlib import Path

from src.core import runtime_state, tool_reservation

logger = logging.getLogger(__name__)

# Mirrors of src/tools/chrome_desktop.py (core must not import from tools/).
_DEBUG_PORT = int(os.environ.get("KBOTS_CHROME_DEBUG_PORT", "9222"))
_RESOURCE = "chrome_browser"

_FLAG = "browser_last_activity"


def _debug_dir() -> Path:
    """The user-data-dir of the debug Chrome (mirror of chrome-debug.sh)."""
    return Path(os.environ.get("KBOTS_CHROME_DEBUG_DIR",
                               str(Path.home() / ".kbots-chrome-debug")))


def _port_up(port: int = _DEBUG_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


async def _close_via_cdp(port: int = _DEBUG_PORT) -> bool:
    """Ask Chrome to shut itself down (Browser.close). True if it obliged."""
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/json/version") as resp:
                ws_url = (await resp.json(content_type=None)).get("webSocketDebuggerUrl", "")
            if not ws_url:
                return False
            async with session.ws_connect(ws_url, max_msg_size=0) as ws:
                await ws.send_json({"id": 1, "method": "Browser.close"})
                # Chrome may drop the socket before replying — that IS success.
                try:
                    await ws.receive(timeout=5)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"browser-janitor: CDP close failed: {e}")
        return False
    for _ in range(10):
        await asyncio.sleep(1)
        if not _port_up(port):
            return True
    return False


async def _close_via_sigterm() -> None:
    """Fallback: TERM every process of the debug profile (helpers included)."""
    proc = await asyncio.create_subprocess_exec(
        "pkill", "-TERM", "-f", f"user-data-dir={_debug_dir()}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()


class BrowserJanitor:
    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.idle_h = float(cfg.get("idle_quit_hours", 3))
        self.tick = float(cfg.get("tick_seconds", 600))

    @property
    def enabled(self) -> bool:
        return self.idle_h > 0

    def _last_activity(self, now: float) -> float | None:
        """High-water mark of browser use: reservation record vs stored flag.

        Returns None on first sighting (flag initialised to `now`), so a
        browser found already running gets a full idle window, never an
        immediate kill.
        """
        try:
            flag = float(runtime_state.get_flag(_FLAG) or 0)
        except (TypeError, ValueError):
            flag = 0.0
        rec = tool_reservation.last_activity(_RESOURCE) or 0.0
        last = max(flag, rec)
        if not last:
            runtime_state.set_flag(_FLAG, now)
            return None
        if last > flag:
            runtime_state.set_flag(_FLAG, last)
        return last

    async def _tick(self, now: float | None = None) -> bool:
        """One housekeeping pass. Returns True when it quit the browser."""
        now = now if now is not None else time.time()
        if not _port_up():
            if runtime_state.get_flag(_FLAG):
                runtime_state.set_flag(_FLAG, 0)   # next launch starts a fresh window
            return False
        if tool_reservation.peek(_RESOURCE) is not None:
            runtime_state.set_flag(_FLAG, now)  # actively driven right now
            return False
        last = self._last_activity(now)
        if last is None or (now - last) < self.idle_h * 3600:
            return False

        idle_for = tool_reservation.format_duration(now - last)
        logger.info(f"browser-janitor: debug Chrome idle {idle_for} "
                    f"(> {self.idle_h:g}h) — closing it")
        if not await _close_via_cdp():
            logger.warning("browser-janitor: falling back to SIGTERM")
            await _close_via_sigterm()
        runtime_state.set_flag(_FLAG, 0)
        return True

    async def run(self) -> None:
        logger.info(f"Browser janitor started (quit after {self.idle_h:g}h idle, "
                    f"check every {self.tick:g}s)")
        while True:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"browser-janitor tick failed: {e}", exc_info=True)
            await asyncio.sleep(self.tick)
