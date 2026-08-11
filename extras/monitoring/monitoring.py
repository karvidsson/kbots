"""Monitoring tools — watch feeds, pages, and weather; pair with the scheduler.

Keyless. Meant to be run on a schedule (see the scheduler/triggers) so agents
can report changes instead of only answering on demand.
- rss_read: latest items from an RSS/Atom feed (stdlib parser, no deps).
- web_watch: detect whether a page changed since the last check (state in
  runtime_state, so it persists across restarts).
- weather: current conditions + short forecast via open-meteo (no API key).
"""

import hashlib
import ipaddress
import logging
import re
import socket
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

from src.core import runtime_state
from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

# A browser User-Agent — some feeds (e.g. BEA) 403 non-browser agents.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/149.0 Safari/537.36")

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


def _validate_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Blocked scheme: {parsed.scheme or '(none)'}. Only http/https allowed."
    host = parsed.hostname
    if not host:
        return "No hostname in URL."
    try:
        addrs = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"Cannot resolve hostname: {host}"
    for *_, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        if any(ip in net for net in _BLOCKED_NETS):
            return f"Blocked: {host} resolves to internal address {ip}."
    return None


async def _fetch(url: str, timeout: int = 20) -> tuple[str | None, str]:
    """Fetch a URL. Returns (text, "") or (None, error)."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(5):  # follow redirects manually, re-validating each hop
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                                 headers={"User-Agent": _UA}, allow_redirects=False) as r:
                    if r.status in (301, 302, 303, 307, 308):
                        loc = r.headers.get("Location")
                        if not loc:
                            return None, f"Redirect without Location from {url}"
                        url = urljoin(url, loc)
                        err = _validate_url(url)  # block SSRF via redirect
                        if err:
                            return None, err
                        continue
                    if r.status >= 400:
                        return None, f"HTTP {r.status} fetching {url}"
                    return await r.text(), ""
            return None, "Too many redirects"
    except (aiohttp.ClientError, TimeoutError) as e:
        return None, f"Fetch failed: {e}"


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _extract_items(text: str) -> tuple[str, list[dict]]:
    """Parse an RSS/Atom document. Returns (feed_title, items) where each item is
    {title, link, date, source}. Raises ET.ParseError on invalid XML."""
    root = ET.fromstring(text)
    feed_title = ""
    for el in root.iter():
        if _strip_ns(el.tag) == "title":
            feed_title = (el.text or "").strip()
            break
    items: list[dict] = []
    for el in root.iter():
        if _strip_ns(el.tag) not in ("item", "entry"):
            continue
        item = {"title": "", "link": "", "date": "", "source": ""}
        for child in el:
            tag = _strip_ns(child.tag)
            if tag == "title" and not item["title"]:
                item["title"] = (child.text or "").strip()
            elif tag == "link" and not item["link"]:
                item["link"] = (child.text or "").strip() or child.get("href", "")
            elif tag in ("pubdate", "published", "updated", "date") and not item["date"]:
                item["date"] = (child.text or "").strip()
            elif tag == "source" and not item["source"]:
                item["source"] = (child.text or "").strip()
        items.append(item)
    return feed_title, items


@tool(name="rss_read", description="Read latest items from an RSS or Atom feed", category="research")
async def rss_read(ctx: ToolContext, url: str, limit: int = 10) -> str:
    """Return the latest entries from an RSS or Atom feed (title, link, date)."""
    err = _validate_url(url)
    if err:
        return err
    text, ferr = await _fetch(url)
    if text is None:
        return ferr
    try:
        feed_title, items = _extract_items(text)
    except ET.ParseError as e:
        return f"Feed is not valid XML: {e}"
    if not items:
        return f"No items found in feed: {url}"

    out = [f"**{feed_title or 'Feed'}** — {min(limit, len(items))} of {len(items)} items:"]
    for it in items[:limit]:
        title, link, date = it["title"], it["link"], it["date"]
        out.append(f"- {title}" + (f" ({date})" if date else "") + (f"\n  {link}" if link else ""))
    return "\n".join(out)


@tool(name="web_watch", description="Detect whether a web page changed since the last check", category="research")
async def web_watch(ctx: ToolContext, url: str, selector: str = "") -> str:
    """Report whether a page's content changed since the previous check.

    State is stored per URL (via runtime_state), so scheduling this tool lets an
    agent notice changes over time. selector: an optional simple substring/regex
    to narrow what's watched (e.g. a price container's surrounding text).
    """
    err = _validate_url(url)
    if err:
        return err
    text, ferr = await _fetch(url)
    if text is None:
        return ferr

    # Reduce to visible-ish text; optionally narrow to a regex match
    body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text, flags=re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    if selector:
        try:
            m = re.search(selector, body)
            if not m:
                return f"Selector /{selector}/ not found on the page."
            body = m.group(0)
        except re.error:
            if selector in body:
                idx = body.index(selector)
                body = body[idx:idx + 500]
            else:
                return f"Selector '{selector}' not found on the page."

    digest = hashlib.sha256(body.encode(errors="replace")).hexdigest()
    key = "web_watch:" + hashlib.sha256((url + "|" + selector).encode()).hexdigest()[:16]
    previous = runtime_state.get_flag(key)

    try:
        runtime_state.set_flag(key, digest)
        persisted = True
    except RuntimeError:
        persisted = False  # KBOTS_OVERLAY not set — can't persist across restarts

    if previous is None:
        note = "" if persisted else " (note: state not persisted — KBOTS_OVERLAY unset)"
        return f"Baseline recorded for {url}. Future checks will report changes.{note}"
    if previous == digest:
        return f"No change since last check: {url}"
    return f"CHANGED since last check: {url}\nNew content preview: {body[:400]}"


# Open-Meteo WMO weather codes → short description
_WMO = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "snow showers", 95: "thunderstorm",
    96: "thunderstorm w/ hail", 99: "thunderstorm w/ hail",
}


@tool(name="weather", description="Current weather and short forecast for a place (no API key)", category="research")
async def weather(ctx: ToolContext, location: str) -> str:
    """Get current conditions and a 3-day forecast for a place name (open-meteo, keyless)."""
    if not location.strip():
        return "Provide a location name."
    import aiohttp
    geo_url = "https://geocoding-api.open-meteo.com/v1/search"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(geo_url, params={"name": location, "count": "1"},
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                geo = await r.json()
    except (aiohttp.ClientError, TimeoutError) as e:
        return f"Geocoding failed: {e}"
    results = geo.get("results") or []
    if not results:
        return f"Could not find a location named '{location}'."
    p = results[0]
    lat, lon = p["latitude"], p["longitude"]
    place = ", ".join(x for x in (p.get("name"), p.get("admin1"), p.get("country")) if x)

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": str(lat), "longitude": str(lon),
                        "current": ("temperature_2m,apparent_temperature,"
                                    "relative_humidity_2m,wind_speed_10m,weather_code"),
                        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                        "timezone": "auto", "forecast_days": "3"},
                timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json()
    except (aiohttp.ClientError, TimeoutError) as e:
        return f"Weather fetch failed: {e}"

    cur = data.get("current", {})
    code = cur.get("weather_code")
    out = [
        f"**Weather — {place}**",
        f"Now: {cur.get('temperature_2m')}°C ({_WMO.get(code, 'code ' + str(code))}), "
        f"feels {cur.get('apparent_temperature')}°C, "
        f"humidity {cur.get('relative_humidity_2m')}%, wind {cur.get('wind_speed_10m')} km/h",
    ]
    daily = data.get("daily", {})
    days = daily.get("time", [])
    if days:
        out.append("Forecast:")
        for i, day in enumerate(days):
            dcode = daily["weather_code"][i]
            out.append(f"- {day}: {daily['temperature_2m_min'][i]}–{daily['temperature_2m_max'][i]}°C, "
                       f"{_WMO.get(dcode, 'code ' + str(dcode))}, "
                       f"{daily['precipitation_probability_max'][i]}% precip")
    return "\n".join(out)
