"""Interactive browser tool — Playwright-based with session persistence.

Supports: open, resize, click, fill, select, screenshot, get_text, scroll,
dismiss_consent, back, close. Sessions persist across calls so agents can
navigate multi-step flows (cookie consent → login → extract data).

Three properties this tool has to have, learned from an agent failing the
simple task "screenshot a news site":

  IT MUST NOT LIE ABOUT WHAT IS ON SCREEN. get_text used to strip iframes out
  of the live DOM and return the text underneath, so a page covered by a
  consent wall read as unblocked. The agent reported "no consent overlay
  present" and was wrong. Worse, the strip was destructive: the iframe was gone
  for every later action in that session, so the dialog could no longer be
  clicked either.

  IT MUST REACH INTO FRAMES. Nearly every EU consent wall renders inside an
  iframe. Without a documented way in, agents rediscover a Playwright internal
  selector by trial and paste it into prompts.

  IT MUST SAY WHAT IT PRODUCED. A screenshot returned a path and nothing else,
  so a 1280x20000 strip looked like a normal result until a human saw a sliver.
"""

import logging
import struct
import time
from pathlib import Path

from src.core.base import KBOTS_TMP, ToolContext
from src.core.tools import tool
from src.lib.ssrf import install_playwright_guard
from src.lib.ssrf import validate_url as _validate_url

logger = logging.getLogger(__name__)

# Session store: {session_id: {browser, context, page, viewport, last_used}}
_sessions: dict[str, dict] = {}
_SESSION_TTL = 300  # 5 min idle timeout

_UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
              "Mobile/15E148 Safari/604.1")
_UA_ANDROID = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/536.36")
_UA_IPAD = ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# An explicit table rather than playwright.devices: the descriptor names move
# between Playwright releases, and a device shorthand that breaks on a
# dependency bump is worse than one that is boring.
DEVICES: dict[str, dict] = {
    "desktop": {"width": 1280, "height": 720, "scale": 1.0, "ua": _UA_DESKTOP},
    "desktop-1920": {"width": 1920, "height": 1080, "scale": 1.0, "ua": _UA_DESKTOP},
    "desktop-1440": {"width": 1440, "height": 900, "scale": 2.0, "ua": _UA_DESKTOP},
    "laptop": {"width": 1366, "height": 768, "scale": 1.0, "ua": _UA_DESKTOP},
    "ipad": {"width": 820, "height": 1180, "scale": 2.0, "ua": _UA_IPAD, "mobile": True},
    "iphone": {"width": 393, "height": 852, "scale": 3.0, "ua": _UA_IPHONE, "mobile": True},
    "android": {"width": 412, "height": 915, "scale": 2.625, "ua": _UA_ANDROID,
                "mobile": True},
}

DEFAULT_VIEWPORT = {"width": 1280, "height": 720, "scale": 1.0, "ua": _UA_DESKTOP,
                    "mobile": False}

# Screenshots taller than this many viewports are tiled instead of returned as
# one strip. A 1280x20000 image is scaled by any chat client to an unreadable
# sliver, and the tool used to give no clue that had happened.
TILE_AFTER_VIEWPORTS = 4
MAX_TILES = 8

# Consent-wall accept controls, most specific first. These cover the platforms
# that actually appear (SourcePoint on the Schibsted titles, OneTrust,
# Cookiebot, Quantcast, plus the generic TCF button classes). Tried in the main
# frame and in every child frame, because that is where they live.
CONSENT_SELECTORS = (
    'button[title*="Godkänn" i]',
    'button[title*="Accept" i]',
    'button.sp_choice_type_11',
    'button#onetrust-accept-btn-handler',
    'button#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
    'button#CybotCookiebotDialogBodyButtonAccept',
    'button[mode="primary"][aria-label*="Accept" i]',
    'button[aria-label*="Accept all" i]',
    'button[aria-label*="Godkänn" i]',
    '.qc-cmp2-summary-buttons button[mode="primary"]',
    'button[data-testid="uc-accept-all-button"]',
)

CONSENT_TEXTS = ("Godkänn alla", "Godkänn", "Accept all", "Accept All",
                 "Accept all cookies", "I accept", "Alle akzeptieren",
                 "Tout accepter", "Godta alle", "Hyväksy kaikki")


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


def resolve_viewport(width: int = 0, height: int = 0, scale: float = 0.0,
                     device: str = "", base: dict | None = None) -> dict | str:
    """Merge explicit dimensions over a device preset over the current session.

    Returns the viewport spec, or an error string naming the known devices.
    Explicit width/height/scale win over `device` so a preset can be nudged
    without defining a new one.
    """
    spec = dict(base or DEFAULT_VIEWPORT)
    if device:
        preset = DEVICES.get(device.lower().strip())
        if not preset:
            return (f"Unknown device: {device}. Known: {', '.join(sorted(DEVICES))}. "
                    f"Or pass width/height/scale directly.")
        spec = {**DEFAULT_VIEWPORT, **preset}
    if width:
        spec["width"] = int(width)
    if height:
        spec["height"] = int(height)
    if scale:
        spec["scale"] = float(scale)

    if not (200 <= spec["width"] <= 3840 and 200 <= spec["height"] <= 3840):
        return (f"Viewport {spec['width']}x{spec['height']} is out of range "
                f"(200-3840 in each dimension).")
    if not (0.5 <= spec["scale"] <= 4.0):
        return f"Device scale {spec['scale']} is out of range (0.5-4.0)."
    return spec


async def _new_context(browser, spec: dict, storage_state=None):
    """A context at `spec`, optionally carrying an existing session's state."""
    kwargs = {
        "user_agent": spec.get("ua", _UA_DESKTOP),
        "viewport": {"width": spec["width"], "height": spec["height"]},
        "device_scale_factor": spec["scale"],
        "is_mobile": bool(spec.get("mobile")),
        "has_touch": bool(spec.get("mobile")),
    }
    if storage_state:
        kwargs["storage_state"] = storage_state
    context = await browser.new_context(**kwargs)
    page = await context.new_page()
    # Re-validate every request (redirects, subresources, JS nav, clicks), not
    # just the initial open — otherwise a page can reach metadata/loopback.
    await install_playwright_guard(page)
    return context, page


async def _get_or_create_session(session_id: str, spec: dict | None = None) -> dict:
    """Get existing session or create a new one at `spec`."""
    await _cleanup_stale()

    if session_id in _sessions:
        _sessions[session_id]["last_used"] = time.time()
        return _sessions[session_id]

    from playwright.async_api import async_playwright

    spec = spec or dict(DEFAULT_VIEWPORT)
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    context, page = await _new_context(browser, spec)

    _sessions[session_id] = {
        "pw": pw,
        "browser": browser,
        "context": context,
        "page": page,
        "viewport": spec,
        "last_used": time.time(),
    }
    return _sessions[session_id]


async def _apply_viewport(sess: dict, spec: dict) -> str:
    """Move a live session to `spec`, keeping cookies and the current page.

    Width and height are a live resize. device_scale_factor is fixed at context
    creation in Playwright, so changing it means a new context: the session's
    storage state and current URL are carried across so "resize" does not
    quietly mean "log out".
    """
    current = sess["viewport"]
    page = sess["page"]
    if abs(float(current["scale"]) - float(spec["scale"])) < 1e-9 and \
            current.get("ua") == spec.get("ua") and \
            bool(current.get("mobile")) == bool(spec.get("mobile")):
        await page.set_viewport_size({"width": spec["width"], "height": spec["height"]})
        sess["viewport"] = spec
        return "resized"

    url = page.url
    try:
        state = await sess["context"].storage_state()
    except Exception:
        state = None
    context, new_page = await _new_context(sess["browser"], spec, storage_state=state)
    if url and url != "about:blank":
        try:
            await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning(f"browser: could not restore {url} after rescale: {e}")
    try:
        await sess["context"].close()
    except Exception:
        pass
    sess["context"], sess["page"], sess["viewport"] = context, new_page, spec
    return "rebuilt (device scale changed; cookies carried over)"


def _png_size(data: bytes) -> tuple[int, int]:
    """Width and height straight out of the PNG IHDR chunk.

    Exact, and needs no image library. The point is that a screenshot result
    always states its dimensions: a full-page capture of a news front page is
    routinely 20000px tall, which any chat client renders as an unreadable
    sliver, and the old result gave no way to know that before sending it.
    """
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return struct.unpack(">II", data[16:24])


def _save_png(data: bytes, session: str, suffix: str = "") -> Path:
    import os
    import tempfile
    media_dir = KBOTS_TMP / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f"browser_screenshot_{session}{suffix}_",
                               suffix=".png", dir=str(media_dir))
    os.close(fd)
    path = Path(tmp)
    path.write_bytes(data)
    return path


def _truncate(text: str, max_length: int = 15000) -> str:
    if len(text) > max_length:
        return text[:max_length] + "\n\n[truncated]"
    return text


async def _overlay_frames(page) -> list[str]:
    """Frames that are plausibly covering the page, described for a human.

    get_text reads the main document, so a modal in an iframe is invisible to
    it. Rather than guess, say what is there and let the agent decide.
    """
    found = []
    try:
        handles = await page.query_selector_all("iframe")
    except Exception:
        return found
    for h in handles[:20]:
        try:
            box = await h.bounding_box()
            if not box or box["width"] < 200 or box["height"] < 150:
                continue
            attrs = await h.evaluate(
                "el => ({id: el.id || '', title: el.title || '', "
                "z: getComputedStyle(el).zIndex, pos: getComputedStyle(el).position})")
            ident = attrs.get("id") or attrs.get("title") or "iframe"
            found.append(f"{ident} ({int(box['width'])}x{int(box['height'])}"
                         f", position={attrs.get('pos')}, z-index={attrs.get('z')})")
        except Exception:
            continue
    return found


def _target(page, selector: str, frame: str):
    """A locator for `selector`, entering `frame` first when one is given."""
    if frame:
        return page.frame_locator(frame).locator(selector)
    return page.locator(selector)


async def _try_consent(scope) -> str:
    """Click the first consent-accept control found in `scope`. '' if none."""
    for sel in CONSENT_SELECTORS:
        try:
            loc = scope.locator(sel).first
            await loc.click(timeout=1500)
            return sel
        except Exception:
            continue
    for label in CONSENT_TEXTS:
        try:
            loc = scope.get_by_role("button", name=label, exact=False).first
            await loc.click(timeout=1200)
            return f'button "{label}"'
        except Exception:
            continue
    return ""


@tool(
    name="browser",
    description=(
        "Interactive browser — open pages, click buttons, fill forms, take screenshots. "
        "Actions: open, resize, click, fill, select, screenshot, get_text, scroll, "
        "dismiss_consent, back, close. Viewport is controllable (width/height/scale "
        "or device=desktop-1920|ipad|iphone|android). Pass frame='iframe[id*=...]' to "
        "click or read inside an iframe, e.g. a cookie wall; dismiss_consent handles "
        "the common consent platforms automatically. Sessions persist across calls."
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
    width: int = 0,
    height: int = 0,
    scale: float = 0.0,
    device: str = "",
    frame: str = "",
    max_height: int = 0,
) -> str:
    """Interactive browser with persistent sessions.

    Args:
        action: open, resize, click, fill, select, screenshot, get_text, scroll, dismiss_consent, back, close
        url: URL to navigate to (for 'open' action)
        selector: CSS selector for the target element (click, fill, select, get_text)
        text: Text to type (for 'fill') or link text to click (for 'click' without selector)
        value: Option value (for 'select' dropdowns)
        session: Session ID — reuse to continue navigating (default: "default")
        full_page: Capture full scrollable page for screenshots (default: viewport only)
        max_length: Max characters for text output (default 15000)
        width: Viewport width in CSS px (open/resize; default 1280)
        height: Viewport height in CSS px (open/resize; default 720)
        scale: Device pixel ratio (open/resize; 2.0 for retina-sharp text)
        device: Shorthand preset — desktop, desktop-1920, desktop-1440, laptop, ipad, iphone, android
        frame: CSS selector for an iframe to act inside (click, fill, select, get_text)
        max_height: Max pixel height of a full_page screenshot before it is split into numbered tiles
    """
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return "Error: playwright not installed. Run: uv add playwright && playwright install chromium"

    action = action.lower().strip()
    valid_actions = ("open", "resize", "click", "fill", "select", "screenshot",
                     "get_text", "scroll", "dismiss_consent", "back", "close")
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

        wants_viewport = bool(width or height or scale or device)
        existing = _sessions.get(session)
        spec = None
        if wants_viewport or action == "resize":
            spec = resolve_viewport(width, height, scale, device,
                                    base=existing["viewport"] if existing else None)
            if isinstance(spec, str):
                return spec

        sess = await _get_or_create_session(session, spec)
        # A session already open at a different size is moved, not ignored.
        if spec and sess["viewport"] != spec:
            how = await _apply_viewport(sess, spec)
            if action == "resize":
                v = sess["viewport"]
                return (f"Viewport now {v['width']}x{v['height']} @ {v['scale']}x "
                        f"({how}). URL: {sess['page'].url}")
        elif action == "resize":
            v = sess["viewport"]
            return f"Viewport already {v['width']}x{v['height']} @ {v['scale']}x."
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
            v = sess["viewport"]
            out = [f"Opened: **{title}**", f"URL: {page.url}",
                   f"Viewport: {v['width']}x{v['height']} @ {v['scale']}x"]
            overlays = await _overlay_frames(page)
            if overlays:
                out.append("Large iframes present (a consent wall or modal may be "
                           "covering the page — try action=dismiss_consent): "
                           + "; ".join(overlays))
            return "\n".join(out)

        elif action == "dismiss_consent":
            tried = await _try_consent(page)
            where = "main page"
            if not tried:
                for f in page.frames:
                    if f == page.main_frame:
                        continue
                    tried = await _try_consent(f)
                    if tried:
                        where = f"frame {f.name or f.url[:60]}"
                        break
            if not tried:
                overlays = await _overlay_frames(page)
                detail = (" Large iframes still present: " + "; ".join(overlays)
                          if overlays else " No large iframes found either.")
                return ("No consent dialog matched the known accept controls." + detail
                        + " Pass frame='iframe[id*=...]' with an explicit selector to "
                          "click it directly.")
            await page.wait_for_timeout(1200)
            remaining = await _overlay_frames(page)
            tail = ("" if not remaining
                    else f" NOTE: large iframes remain: {'; '.join(remaining)}")
            return f"Consent accepted via {tried} in the {where}.{tail}"

        elif action == "click":
            if selector:
                await _target(page, selector, frame).first.click(timeout=10000)
            elif text:
                scope = page.frame_locator(frame) if frame else page
                clicked = False
                for sel in [f"a:has-text('{text}')", f"button:has-text('{text}')",
                            f"text='{text}'"]:
                    try:
                        await (scope.locator(sel).first if frame
                               else page.locator(sel).first).click(timeout=5000)
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    hint = ""
                    if not frame:
                        overlays = await _overlay_frames(page)
                        if overlays:
                            hint = (" A large iframe is present, and text search does "
                                    "not enter frames: " + "; ".join(overlays)
                                    + ". Retry with frame='iframe[id*=...]', or use "
                                      "action=dismiss_consent.")
                    return f"Could not find clickable element with text: {text}.{hint}"
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
            await _target(page, selector, frame).first.fill(text, timeout=10000)
            return f"Filled '{selector}' with text."

        elif action == "select":
            if not selector:
                return "Error: 'selector' required for select."
            if not value:
                return "Error: 'value' required for select."
            await _target(page, selector, frame).first.select_option(value, timeout=10000)
            return f"Selected '{value}' in '{selector}'."

        elif action == "screenshot":
            v = sess["viewport"]
            limit = max_height or (v["height"] * TILE_AFTER_VIEWPORTS)
            page_height = 0
            if full_page:
                page_height = await page.evaluate(
                    "() => Math.max(document.body ? document.body.scrollHeight : 0,"
                    " document.documentElement.scrollHeight)")

            title = await page.title()
            header = f"Page: **{title}** | URL: {page.url}"

            if full_page and page_height > limit:
                # Tiles rather than one strip: a 1280x20000 image is scaled to an
                # unreadable sliver by every chat client, and it read as a normal
                # result because nothing said how tall it was.
                width_css = await page.evaluate(
                    "() => document.documentElement.clientWidth")
                tiles, y, n = [], 0, 0
                while y < page_height and n < MAX_TILES:
                    h = min(limit, page_height - y)
                    data = await page.screenshot(full_page=True, clip={
                        "x": 0, "y": y, "width": width_css, "height": h})
                    p = _save_png(data, session, suffix=f"_tile{n + 1}")
                    px = _png_size(data)
                    tiles.append(f"  {n + 1}. {p}  ({px[0]}x{px[1]}px)")
                    y += h
                    n += 1
                covered = min(y, page_height)
                note = ""
                if covered < page_height:
                    note = (f"\nNOTE: stopped at {MAX_TILES} tiles — "
                            f"{page_height - covered}px of the page below is NOT "
                            f"captured. Raise max_height to cover it in fewer tiles.")
                return (f"Full page is {width_css}x{page_height}px, split into "
                        f"{len(tiles)} tiles (top to bottom):\n"
                        + "\n".join(tiles) + f"\n{header}{note}")

            data = await page.screenshot(full_page=full_page)
            path = _save_png(data, session)
            px = _png_size(data)
            return (f"Screenshot saved: {path}\nImage: {px[0]}x{px[1]}px "
                    f"(viewport {v['width']}x{v['height']} @ {v['scale']}x"
                    f"{', full page' if full_page else ''})\n{header}")

        elif action == "get_text":
            if selector:
                loc = _target(page, selector, frame)
                count = await loc.count()
                if not count:
                    where = f" inside frame '{frame}'" if frame else ""
                    return f"No elements found for selector: {selector}{where}"
                texts = []
                for i in range(min(count, 50)):
                    t = await loc.nth(i).inner_text()
                    if t.strip():
                        texts.append(t.strip())
                return (_truncate("\n".join(texts), max_length) if texts
                        else "Elements found but no text content.")

            if frame:
                body = page.frame_locator(frame).locator("body")
                if not await body.count():
                    return f"No frame matched: {frame}"
                return _truncate(await body.first.inner_text(), max_length)

            # innerText is what a user would see: it already excludes script,
            # style and noscript because they are not rendered, and iframe
            # content is never part of it. The old implementation removed those
            # elements from the LIVE DOM first, which was both unnecessary and
            # destructive — the consent iframe it deleted could not be clicked
            # afterwards, in this or any later call on the same session.
            page_text = (await page.evaluate(
                "() => document.body ? document.body.innerText : ''")).strip()
            title = await page.title()
            if not page_text:
                return f"No text content on page: {page.url}"
            result = f"**{title}**\n\n{page_text}" if title else page_text

            overlays = await _overlay_frames(page)
            if overlays:
                # Say it BEFORE the text. This is the exact failure that made an
                # agent report "no consent overlay present" about a page it could
                # not see: the text below is the document underneath the modal.
                result = ("⚠️ The text below is the MAIN DOCUMENT ONLY. Large "
                          "iframes are present and their content is not included, "
                          "so a modal or consent wall may be covering this page: "
                          + "; ".join(overlays)
                          + "\nUse action=dismiss_consent, or frame='iframe[id*=...]' "
                            "to read inside one.\n\n" + result)
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
