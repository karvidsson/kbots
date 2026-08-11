"""Interactive browser tool — Playwright-based with session persistence.

Supports: open, click, fill, select, screenshot, get_text, scroll, wait.
Sessions persist across calls so agents can navigate multi-step flows
(cookie consent → login → extract data).
"""

import ipaddress
import logging
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

from src.core.base import KBOTS_TMP, ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

# SSRF protection — block internal networks
_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

# Session store: {session_id: {browser, context, page, last_used}}
_sessions: dict[str, dict] = {}
_SESSION_TTL = 300  # 5 min idle timeout


def _validate_url(url: str) -> str | None:
    """Block internal/private URLs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Blocked scheme: {parsed.scheme}. Only http/https allowed."
    hostname = parsed.hostname
    if not hostname:
        return "No hostname in URL."
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return f"Cannot resolve hostname: {hostname}"
    for _, _, _, _, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        # An IPv4-mapped IPv6 address (::ffff:a.b.c.d) never matches the IPv4
        # nets directly — check the embedded IPv4 as well.
        mapped = getattr(ip, "ipv4_mapped", None)
        candidates = [ip] + ([mapped] if mapped else [])
        for cand in candidates:
            for net in _BLOCKED_NETS:
                if cand in net:
                    return f"Blocked: {hostname} resolves to internal address {ip}."
    return None


async def _cleanup_stale():
    """Close sessions idle longer than TTL."""
    now = time.time()
    stale = [sid for sid, s in _sessions.items() if now - s["last_used"] > _SESSION_TTL]
    for sid in stale:
        try:
            await _sessions[sid]["browser"].close()
        except Exception:
            pass
        del _sessions[sid]
    if stale:
        logger.info(f"browser: cleaned up {len(stale)} stale session(s)")


async def _get_or_create_session(session_id: str) -> dict:
    """Get existing session or create a new one."""
    await _cleanup_stale()

    if session_id in _sessions:
        _sessions[session_id]["last_used"] = time.time()
        return _sessions[session_id]

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 720},
    )
    page = await context.new_page()

    _sessions[session_id] = {
        "pw": pw,
        "browser": browser,
        "context": context,
        "page": page,
        "last_used": time.time(),
    }
    return _sessions[session_id]


def _truncate(text: str, max_length: int = 15000) -> str:
    if len(text) > max_length:
        return text[:max_length] + "\n\n[truncated]"
    return text


@tool(
    name="browser",
    description=(
        "Interactive browser — open pages, click buttons, fill forms, take screenshots. "
        "Actions: open, click, fill, select, screenshot, get_text, scroll, back, close. "
        "Sessions persist across calls for multi-step navigation."
    ),
    category="research",
)
async def browser(
    ctx: ToolContext,
    action: str,
    url: str = "",
    selector: str = "",
    text: str = "",
    value: str = "",
    session: str = "default",
    full_page: bool = False,
    max_length: int = 15000,
) -> str:
    """Interactive browser with persistent sessions.

    Args:
        action: What to do — open, click, fill, select, screenshot, get_text, scroll, back, close
        url: URL to navigate to (for 'open' action)
        selector: CSS selector for the target element (click, fill, select, get_text)
        text: Text to type (for 'fill') or link text to click (for 'click' without selector)
        value: Option value (for 'select' dropdowns)
        session: Session ID — reuse to continue navigating (default: "default")
        full_page: Capture full scrollable page for screenshots (default: viewport only)
        max_length: Max characters for text output (default 15000)
    """
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return "Error: playwright not installed. Run: uv add playwright && playwright install chromium"

    action = action.lower().strip()
    valid_actions = ("open", "click", "fill", "select", "screenshot", "get_text", "scroll", "back", "close")
    if action not in valid_actions:
        return f"Unknown action: {action}. Valid: {', '.join(valid_actions)}"

    try:
        if action == "close":
            if session in _sessions:
                await _sessions[session]["browser"].close()
                await _sessions[session]["pw"].stop()
                del _sessions[session]
                return f"Session '{session}' closed."
            return f"No session '{session}' to close."

        sess = await _get_or_create_session(session)
        page = sess["page"]

        if action == "open":
            if not url:
                return "Error: 'url' required for open action."
            err = _validate_url(url)
            if err:
                return err
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            title = await page.title()
            current_url = page.url
            return f"Opened: **{title}**\nURL: {current_url}"

        elif action == "click":
            if selector:
                await page.click(selector, timeout=10000)
            elif text:
                # Click by visible text — try link, button, then any element
                clicked = False
                for sel in [f"a:has-text('{text}')", f"button:has-text('{text}')", f"text='{text}'"]:
                    try:
                        await page.click(sel, timeout=5000)
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    return f"Could not find clickable element with text: {text}"
            else:
                return "Error: 'selector' or 'text' required for click."
            await page.wait_for_timeout(1500)
            title = await page.title()
            return f"Clicked. Page: **{title}** | URL: {page.url}"

        elif action == "fill":
            if not selector:
                return "Error: 'selector' required for fill (e.g. 'input[name=email]', '#search')."
            if text is None:
                return "Error: 'text' required for fill."
            await page.fill(selector, text, timeout=10000)
            return f"Filled '{selector}' with text."

        elif action == "select":
            if not selector:
                return "Error: 'selector' required for select."
            if not value:
                return "Error: 'value' required for select."
            await page.select_option(selector, value, timeout=10000)
            return f"Selected '{value}' in '{selector}'."

        elif action == "screenshot":
            screenshot_bytes = await page.screenshot(full_page=full_page)
            # Save to KBOTS_TMP for Gemini/vision analysis
            import os
            import tempfile
            media_dir = KBOTS_TMP / "media"
            media_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f"browser_screenshot_{session}_", suffix=".png", dir=str(media_dir))
            os.close(fd)
            path = Path(tmp)
            path.write_bytes(screenshot_bytes)
            title = await page.title()
            return f"Screenshot saved: {path}\nPage: **{title}** | URL: {page.url}"

        elif action == "get_text":
            if selector:
                elements = await page.query_selector_all(selector)
                if not elements:
                    return f"No elements found for selector: {selector}"
                texts = []
                for el in elements[:50]:  # cap at 50 elements
                    t = await el.inner_text()
                    if t.strip():
                        texts.append(t.strip())
                return _truncate("\n".join(texts), max_length) if texts else "Elements found but no text content."
            else:
                # Full page text
                page_text = await page.evaluate("""() => {
                    for (const el of document.querySelectorAll(
                        'script, style, noscript, iframe, svg'
                    )) { el.remove(); }
                    return document.body ? document.body.innerText : '';
                }""")
                title = await page.title()
                page_text = page_text.strip()
                if not page_text:
                    return f"No text content on page: {page.url}"
                result = f"**{title}**\n\n{page_text}" if title else page_text
                return _truncate(result, max_length)

        elif action == "scroll":
            direction = text.lower() if text else "down"
            if direction == "up":
                await page.evaluate("window.scrollBy(0, -500)")
            elif direction == "bottom":
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif direction == "top":
                await page.evaluate("window.scrollTo(0, 0)")
            else:
                await page.evaluate("window.scrollBy(0, 500)")
            await page.wait_for_timeout(500)
            return f"Scrolled {direction}."

        elif action == "back":
            await page.go_back(timeout=10000)
            await page.wait_for_timeout(1500)
            title = await page.title()
            return f"Back. Page: **{title}** | URL: {page.url}"

    except Exception as e:
        return f"Browser error: {e}"
