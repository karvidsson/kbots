"""Real-Chrome tool — drive the actual Google Chrome on macOS via CDP.

The regular `browser` tool uses a headless bundled Chromium, which many sites
block (bot-detection / "agent firewalls"). This tool attaches to the real
Google Chrome over the DevTools protocol, so pages see a genuine browser
fingerprint and your real logged-in sessions.

macOS only. The Chrome instance is started by scripts/chrome-debug.sh, which
runs a separate Chrome on a debug profile seeded from your real one (Chrome 136+
refuses remote debugging on the default profile dir). This tool auto-launches
that helper on first use if the debug port is not already open.

Actions mirror the `browser` tool: open, click, fill, get_text, screenshot,
scroll, back, status, close — plus 'login' for the sign-in-once flow.

There is only one real Chrome, so agents take turns: driving actions reserve the
tool cross-process (see core/tool_reservation) and a second agent is refused
immediately rather than silently steering the first one's tab.

Named profiles (the `profile` arg) give each identity/service its own Chrome
profile inside the same instance and debug port. A profile starts with no
logins; action='login' opens a visible window where the USER signs in by hand —
credentials never pass through the agent — and the session then persists for
the agent across restarts. Revoking = deleting the profile's directory.
"""

import asyncio
import ipaddress
import logging
import os
import platform
import re
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from src.core import tool_reservation
from src.core.base import KBOTS_TMP, PROJECT_ROOT, ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

DEBUG_PORT = int(os.environ.get("KBOTS_CHROME_DEBUG_PORT", "9222"))
_HELPER = PROJECT_ROOT / "scripts" / "chrome-debug.sh"

VALID_ACTIONS = ("open", "login", "click", "fill", "get_text", "screenshot",
                 "scroll", "back", "status", "close", "release", "grant", "revoke")

# Named profiles live inside the debug user-data-dir (one per identity/service,
# e.g. 'flow', 'tiktok'). Must match the allow-list in scripts/chrome-debug.sh.
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,31}$")

# Driving the real browser touches the user's logged-in sessions, so it needs
# explicit consent — asked once per session/task, then free (see session_consent).
CHROME_CAP = "chrome_browser"
_FREE_ACTIONS = ("status", "close", "release", "grant", "revoke")  # no grant needed

# All agents share the one real Chrome, so driving actions take turns on a
# cross-process reservation. Bookkeeping actions don't touch a page, so they
# stay free — 'status' must stay answerable while another agent is driving.
RESOURCE = "chrome_browser"
_RESERVED_ACTIONS = ("open", "login", "click", "fill", "get_text", "screenshot", "scroll", "back")
_ALTERNATIVE = ("Use the `browser` tool instead — it is headless and per-session "
                "isolated, so it is safe to run concurrently.")

# SSRF protection — same blocklist as the browser tool
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

# Single attached-Chrome connection, reused across calls.
# _state["profile_pages"] maps profile name -> pinned Page for that profile's window.
_state: dict = {}


def _debug_dir() -> Path:
    """The user-data-dir of the debug Chrome (mirror of chrome-debug.sh)."""
    return Path(os.environ.get("KBOTS_CHROME_DEBUG_DIR",
                               str(Path.home() / ".kbots-chrome-debug")))


def _validate_url(url: str) -> str | None:
    """Block internal/private URLs (SSRF)."""
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


def _port_up() -> bool:
    """True if the Chrome debug endpoint is listening."""
    try:
        with socket.create_connection(("127.0.0.1", DEBUG_PORT), timeout=1):
            return True
    except OSError:
        return False


async def _ensure_chrome(auto_launch: bool) -> str | None:
    """Make sure a debug Chrome is running. Returns an error string, or None."""
    if _port_up():
        return None
    if not auto_launch:
        return (f"No debug Chrome on port {DEBUG_PORT}. "
                f"Start it with: scripts/chrome-debug.sh")
    if not _HELPER.exists():
        return f"Helper not found: {_HELPER}"
    logger.info("chrome_desktop: launching debug Chrome via helper")
    try:
        # Helper backgrounds Chrome and waits for the port; give it headroom.
        proc = await asyncio.create_subprocess_exec(
            "bash", str(_HELPER),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
    except asyncio.TimeoutError:
        return "Timed out launching debug Chrome (scripts/chrome-debug.sh)."
    except Exception as e:
        return f"Failed to launch debug Chrome: {e}"
    if not _port_up():
        tail = (out or b"").decode(errors="replace")[-300:]
        return f"Debug Chrome did not come up on port {DEBUG_PORT}. Helper output:\n{tail}"
    return None


async def _connect():
    """Attach to the debug Chrome, reusing a live connection. Returns page or error str."""
    # Reuse if the existing connection is still alive
    if _state.get("browser") and _state["browser"].is_connected():
        _state["last_used"] = time.time()
        return _state["page"]

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{DEBUG_PORT}", timeout=15000)
    except Exception as e:
        await pw.stop()
        return f"Could not attach to Chrome on port {DEBUG_PORT}: {e}"

    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    pages = context.pages
    page = pages[0] if pages else await context.new_page()
    _state.update(pw=pw, browser=browser, context=context, page=page,
                  last_used=time.time())
    return page


async def _profile_page(profile: str, create: bool = False):
    """Return the pinned page for a named profile, opening its window if allowed.

    One CDP port serves every profile in the user-data-dir, so pages from all
    profiles share the one Playwright connection. Chrome doesn't label targets
    with their profile, so we pin the page at window-creation time instead:
    snapshot the pages we can see, hand off to chrome-debug.sh --profile (which
    opens a window on that profile in the running instance), and pin whichever
    page appears that wasn't there before.
    """
    pages = _state.setdefault("profile_pages", {})
    page = pages.get(profile)
    if page is not None:
        try:
            if not page.is_closed():
                return page
        except Exception:
            pass
        del pages[profile]
    if not create:
        return (f"No open window for profile '{profile}'. Run "
                f"chrome_browser(action='open', profile='{profile}', url=...) first.")

    browser = _state.get("browser")
    if not browser or not browser.is_connected():
        return "Not attached to Chrome — retry the action."
    before = {p for ctx in browser.contexts for p in ctx.pages}
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(_HELPER), "--profile", profile, "--open", "about:blank",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception as e:
        return f"Could not open a window on profile '{profile}': {e}"
    for _ in range(60):  # up to 15s for the new target to surface over CDP
        await asyncio.sleep(0.25)
        fresh = [p for ctx in browser.contexts for p in ctx.pages if p not in before]
        if fresh:
            pages[profile] = fresh[-1]
            return fresh[-1]
    return (f"Opened a window on profile '{profile}' but its tab never surfaced "
            f"over CDP. Check action='status' and retry.")


async def _teardown() -> None:
    """Detach from Chrome (leaves the browser running)."""
    pw = _state.get("pw")
    browser = _state.get("browser")
    try:
        if browser and browser.is_connected():
            await browser.close()  # closes the CDP connection, not the Chrome app
    except Exception:
        pass
    try:
        if pw:
            await pw.stop()
    except Exception:
        pass
    _state.clear()


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… [truncated at {limit} chars]"


@tool(
    name="chrome_browser",
    description=(
        "Drive the real Google Chrome (macOS) to reach sites that block the headless "
        "browser tool. Uses your genuine browser fingerprint and logged-in sessions. "
        "One shared browser: it reserves itself while you use it and refuses other "
        "agents until you release or go idle — call action='release' when done. "
        "Named profiles keep identities apart: pass profile='name' to work in a "
        "dedicated profile, and use action='login' to have the user sign in to it "
        "by hand (their credentials never pass through you; the session persists). "
        "Actions: open, login, click, fill, get_text, screenshot, scroll, back, "
        "status, release, close."
    ),
    category="browser",
)
async def chrome_browser(ctx: ToolContext, action: str, url: str = "", selector: str = "",
                         text: str = "", profile: str = "", max_length: int = 15000,
                         full_page: bool = False, auto_launch: bool = True) -> str:
    """Drive the real desktop Google Chrome via the DevTools protocol.

    Use this instead of `browser` when a site blocks automated/headless browsers
    or needs one of your existing logins. On first use it launches a debug Chrome
    (scripts/chrome-debug.sh) seeded from your real profile; a visible window
    opens so you can complete any login or captcha the agent can't.

    PERMISSION: because this uses the user's real logged-in browser, it requires
    one-time-per-session consent. The first action returns "PERMISSION NEEDED" —
    ask the user, and only after they agree call action='grant', then retry.
    Consent lasts the task (~8h); action='revoke' clears it.

    SHARING: there is only one real Chrome and every agent drives the same tab,
    so the first driving action reserves it. Another agent gets a clear refusal
    (pointing them at `browser`) until you release it or leave it idle for 10
    minutes. **Call action='release' as soon as you're done** — nothing fires at
    the end of your turn, so until then only the idle timeout frees it.

    PROFILES: pass profile='name' to drive a dedicated Chrome profile instead of
    the shared Default one — one profile per identity/service (e.g. 'flow',
    'tiktok') so logins never cross. A fresh profile has no sessions; to get one,
    use action='login' with the sign-in URL: a visible window opens and the USER
    signs in there by hand (2FA and all — credentials never pass through you or
    the vault). The session then persists in that profile across restarts; the
    user can revoke it any time by deleting the profile's directory. If a page
    unexpectedly shows logged-out, suspect the wrong profile before assuming the
    session is lost — check action='status' for which profiles exist.

    Args:
        action: open, login (open a window for the user to sign in by hand),
            click, fill, get_text, screenshot, scroll, back, status,
            release (give up your reservation), close (detach + release).
        url: URL to open (for 'open' and 'login').
        selector: CSS selector for the target (click, fill, get_text).
        text: text to type (for 'fill') or visible link/button text (for 'click' without selector).
        profile: named Chrome profile to drive ('' = the default debug profile).
            Required for 'login'. Letters/digits/space/-/_ only.
        max_length: max characters of text to return (get_text).
        full_page: capture the full scrollable page (screenshot).
        auto_launch: start the debug Chrome automatically if it isn't running (default true).
    """
    if platform.system() != "Darwin":
        return "chrome_browser is macOS-only (it drives the desktop Google Chrome app)."

    action = action.lower().strip()
    if action not in VALID_ACTIONS:
        return f"Unknown action: {action}. Valid: {', '.join(VALID_ACTIONS)}"

    profile = profile.strip()
    if profile and not _PROFILE_RE.match(profile):
        return (f"Invalid profile name: '{profile}'. Use letters, digits, space, "
                f"- and _ (max 32 chars).")
    if action == "login":
        if not profile:
            return ("Error: 'profile' required for login — name it after the "
                    "identity/service, e.g. profile='flow'.")
        if not url:
            return "Error: 'url' required for login (the sign-in page to show the user)."

    if action == "status":
        holder = tool_reservation.peek(RESOURCE)
        if holder and holder.agent_id != ctx.agent_id:
            who = (f"Reserved by '{holder.agent_id}' "
                   f"(idle {tool_reservation.format_duration(holder.idle_for())}, "
                   f"expires in {tool_reservation.format_duration(holder.expires_in())}).")
        elif holder:
            who = f"Reserved by you since {tool_reservation.format_duration(holder.idle_for())} of idle."
        else:
            who = "Not reserved — free to use."
        profs = ""
        d = _debug_dir()
        if d.is_dir():
            names = sorted(p.name for p in d.iterdir() if (p / "Preferences").exists())
            if names:
                pinned = set()
                for name, pg in (_state.get("profile_pages") or {}).items():
                    try:
                        if pg and not pg.is_closed():
                            pinned.add(name)
                    except Exception:
                        pass
                profs = " Profiles: " + ", ".join(
                    n + (" (window pinned)" if n in pinned else "") for n in names) + "."
        if _port_up():
            attached = bool(_state.get("browser") and _state["browser"].is_connected())
            return f"Debug Chrome is up on port {DEBUG_PORT} (attached: {attached}). {who}{profs}"
        return (f"Debug Chrome is not running on port {DEBUG_PORT}. "
                f"An 'open' will launch it. {who}{profs}")

    if action == "release":
        freed = tool_reservation.release(RESOURCE, ctx.agent_id)
        return ("Released the chrome_browser reservation — another agent can use it now."
                if freed else "You did not hold the chrome_browser reservation.")

    if action == "close":
        await _teardown()
        tool_reservation.release(RESOURCE, ctx.agent_id)
        return "Detached from Chrome and released the reservation (the browser window is left running)."

    from src.core import session_consent
    if action == "grant":
        session_consent.grant(ctx.agent_id, CHROME_CAP)
        hrs = round(session_consent.DEFAULT_TTL / 3600)
        return (f"✅ Chrome access granted for this session (~{hrs}h). I can now drive "
                f"your browser without re-asking. Revoke anytime with action='revoke'.")
    if action == "revoke":
        session_consent.revoke(ctx.agent_id, CHROME_CAP)
        return "Chrome access revoked — I'll ask again before using your browser."

    # Consent gate: ask the user once per session before touching their real browser.
    if not session_consent.is_granted(ctx.agent_id, CHROME_CAP):
        return (
            "🔒 PERMISSION NEEDED — this drives your REAL Chrome with your logged-in "
            "sessions. Ask the user for permission **once for this task**; only after "
            "they agree, call chrome_browser(action='grant') and then retry your action. "
            "Do not grant without asking."
        )

    # Turn-taking gate: claim the shared Chrome before touching a page. Checked
    # ahead of _ensure_chrome so a refused agent never launches anything. Each
    # call refreshes the heartbeat, so a long active session can't expire on us.
    if action in _RESERVED_ACTIONS:
        try:
            ok, holder = tool_reservation.acquire(RESOURCE, ctx.agent_id)
        except (TimeoutError, OSError) as e:
            return f"Could not check the chrome_browser reservation: {e}"
        if not ok:
            return tool_reservation.busy_message(holder, _ALTERNATIVE)

    launch_err = await _ensure_chrome(auto_launch)
    if launch_err:
        return launch_err

    page = await _connect()
    if isinstance(page, str):
        return page

    # Point the action at the named profile's window. 'open'/'login' may create
    # it; everything else requires it to already exist so a click can never
    # silently land in a window the agent didn't set up.
    if profile:
        page = await _profile_page(profile, create=action in ("open", "login"))
        if isinstance(page, str):
            return page

    try:
        if action in ("open", "login"):
            if not url:
                return "Error: 'url' required for open."
            err = _validate_url(url)
            if err:
                return err
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)
            if action == "login":
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                return (
                    f"🪟 Sign-in window ready on profile '{profile}': "
                    f"**{await page.title()}** | {page.url}\n"
                    f"Ask the user to sign in **in that window** — they type their "
                    f"password (and 2FA) directly into Chrome; you never see or store "
                    f"credentials. When they confirm, continue with normal actions "
                    f"passing profile='{profile}'. The login persists across restarts; "
                    f"the user can revoke it by deleting {_debug_dir() / profile}."
                )
            return f"Opened: **{await page.title()}**\nURL: {page.url}"

        if action == "click":
            if selector:
                await page.click(selector, timeout=15000)
            elif text:
                clicked = False
                for sel in (f"a:has-text('{text}')", f"button:has-text('{text}')", f"text='{text}'"):
                    try:
                        await page.click(sel, timeout=5000)
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    return f"Could not find anything to click matching: {text}"
            else:
                return "Error: 'selector' or 'text' required for click."
            await page.wait_for_timeout(1200)
            return f"Clicked. Now on: **{await page.title()}** | {page.url}"

        if action == "fill":
            if not selector:
                return "Error: 'selector' required for fill."
            await page.fill(selector, text, timeout=15000)
            return f"Filled {selector}."

        if action == "scroll":
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(600)
            return f"Scrolled. URL: {page.url}"

        if action == "back":
            await page.go_back(wait_until="domcontentloaded")
            await page.wait_for_timeout(800)
            return f"Went back to: **{await page.title()}** | {page.url}"

        if action == "screenshot":
            media_dir = KBOTS_TMP / "media"
            media_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="chrome_screenshot_", suffix=".png", dir=str(media_dir))
            os.close(fd)
            out = Path(tmp)
            await page.screenshot(path=str(out), full_page=full_page)
            return f"Screenshot saved: {out}\nPage: **{await page.title()}** | URL: {page.url}"

        if action == "get_text":
            if selector:
                elements = await page.query_selector_all(selector)
                if not elements:
                    return f"No elements found for selector: {selector}"
                texts = [t.strip() for el in elements[:50]
                         if (t := await el.inner_text()).strip()]
                return _truncate("\n".join(texts), max_length) if texts else "Elements found but no text."
            body = await page.evaluate(
                """() => { for (const el of document.querySelectorAll(
                    'script, style, noscript, iframe, svg')) el.remove();
                    return document.body ? document.body.innerText : ''; }""")
            return _truncate(body.strip(), max_length) or "No text on page."
    except Exception as e:
        return f"chrome_browser {action} failed: {e}"

    return f"Action '{action}' completed."
